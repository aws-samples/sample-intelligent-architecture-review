# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Amazon Bedrock Guardrail application and intervention accounting.

Every model call in this service goes through `Converse` or `ConverseStream`, and
the guardrail has to be attached to all of them — a call that omits
`guardrailConfig` is simply unguarded, with no error to indicate it.

Rather than adding the argument at each of the fifteen call sites, this module
wraps the client. A wrapped client injects `guardrailConfig` on every
`converse`/`converse_stream` call and records what the guardrail did on the way
back. Adding a new call site therefore inherits the guardrail by construction,
and a future call site that builds its own client is the only way to bypass it.

Configuration comes from the environment, which Terraform populates from the
guardrail module:

    BEDROCK_GUARDRAIL_ID        guardrail identifier
    BEDROCK_GUARDRAIL_VERSION   a numbered version, never DRAFT
    BEDROCK_GUARDRAIL_TRACE     enabled | disabled  (default enabled)

When the id is absent the wrapper is a pass-through. That keeps local development
and a deliberately disabled environment on the same code path, rather than making
an unconfigured guardrail a startup failure.

The policy itself is deliberately narrow — PII and credential redaction plus
prompt-attack detection on input, with no content filters and no denied topics.
This service reads compliance material that legitimately discusses firewalls, key
management, and how a control could be bypassed. See
infrastructure/modules/bedrock-guardrail/main.tf for the full reasoning.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Guardrail actions Bedrock reports on a response.
_ACTION_BLOCKED = "BLOCKED"


def guardrail_id() -> str:
    return (os.getenv("BEDROCK_GUARDRAIL_ID", "") or "").strip()


def guardrail_version() -> str:
    # Default to "DRAFT" only so a manually-created guardrail can be tried
    # locally; Terraform always supplies a numbered version.
    return (os.getenv("BEDROCK_GUARDRAIL_VERSION", "") or "DRAFT").strip()


def is_enabled() -> bool:
    """True when a guardrail is configured. Read per call rather than cached at
    import, so a test can set the variable without reimporting the module."""
    return bool(guardrail_id())


def guardrail_config() -> dict:
    """The `guardrailConfig` argument for Converse, or an empty dict.

    Returned as a dict to splat so a caller reads
    `client.converse(**base, **guardrail_config())` and gets nothing added when
    no guardrail is configured.
    """
    gid = guardrail_id()
    if not gid:
        return {}
    trace = (os.getenv("BEDROCK_GUARDRAIL_TRACE", "enabled") or "enabled").strip().lower()
    return {
        "guardrailConfig": {
            "guardrailIdentifier": gid,
            "guardrailVersion": guardrail_version(),
            # The trace is what lets an intervention be attributed to a specific
            # policy rather than appearing as an unexplained empty response.
            "trace": "enabled" if trace != "disabled" else "disabled",
        }
    }


def strands_guardrail_kwargs() -> dict:
    """Guardrail settings for a Strands `BedrockModel`.

    Strands takes flat keyword arguments rather than the Converse
    `guardrailConfig` shape, so the two paths cannot share one dict.
    """
    gid = guardrail_id()
    if not gid:
        return {}
    return {
        "guardrail_id": gid,
        "guardrail_version": guardrail_version(),
        "guardrail_trace": "enabled",
    }


def describe_intervention(response: Any) -> Optional[dict]:
    """Summarize what the guardrail did to one response, or None if it did
    nothing.

    Bedrock reports `stopReason == "guardrail_intervened"` when a call was
    blocked, and includes an assessment in `trace.guardrail` whenever the trace
    is enabled — including for a redaction, which is not a stop reason. Both
    matter: a block produces no verdict, and a redaction changes what the model
    saw.
    """
    if not isinstance(response, dict):
        return None

    stop_reason = str(response.get("stopReason", "") or "")
    intervened = stop_reason.lower() == "guardrail_intervened"

    trace = (response.get("trace") or {}).get("guardrail") or {}
    policies: set[str] = set()
    # Bedrock reports stopReason=guardrail_intervened for a REDACTION as well as
    # for a block, so the stop reason alone cannot distinguish them. Verified
    # against a live guardrail: an anonymized email returns that stop reason with
    # the response text intact and the entity replaced by {EMAIL}. Treating both
    # as "blocked" would fire the blocked-call alarm on every redaction, which is
    # the expected case rather than the exceptional one.
    #
    # A block is therefore identified by the action reported on the assessment,
    # not by the stop reason.
    actions: set[str] = set()
    # inputAssessment is keyed by guardrail id; outputAssessments is a list.
    assessments: list[dict] = []
    input_assessment = trace.get("inputAssessment") or {}
    if isinstance(input_assessment, dict):
        assessments.extend(v for v in input_assessment.values() if isinstance(v, dict))
    output_assessments = trace.get("outputAssessments") or {}
    if isinstance(output_assessments, dict):
        for v in output_assessments.values():
            if isinstance(v, list):
                assessments.extend(x for x in v if isinstance(x, dict))
    elif isinstance(output_assessments, list):
        assessments.extend(x for x in output_assessments if isinstance(x, dict))

    def _collect_actions(policy: dict, *keys: str) -> None:
        for key in keys:
            for entry in policy.get(key) or []:
                if isinstance(entry, dict) and entry.get("action"):
                    actions.add(str(entry["action"]).upper())

    for a in assessments:
        si = a.get("sensitiveInformationPolicy")
        if si:
            policies.add("sensitive_information")
            _collect_actions(si, "piiEntities", "regexes")
        cp = a.get("contentPolicy")
        if cp:
            policies.add("content")
            _collect_actions(cp, "filters")
        tp = a.get("topicPolicy")
        if tp:
            policies.add("topic")
            _collect_actions(tp, "topics")
        wp = a.get("wordPolicy")
        if wp:
            policies.add("word")
            _collect_actions(wp, "customWords", "managedWordLists")

    if not intervened and not policies:
        return None

    # ANONYMIZED / NONE mean the content was rewritten and the call completed.
    # BLOCKED means no usable response was produced.
    blocked = "BLOCKED" in actions
    if not actions and intervened and not policies:
        # Intervened with no assessment detail (trace disabled): the safe reading
        # is that the call was blocked, since a redaction would have reported one.
        blocked = True

    return {
        "blocked": blocked,
        "policies": sorted(policies),
        "actions": sorted(actions),
        "action": _ACTION_BLOCKED if blocked else "REDACTED",
    }


def _emit_metrics(intervention: dict) -> None:
    """Emit intervention counters. Best-effort: telemetry must never break a call
    that otherwise succeeded."""
    try:
        from app.services.metrics import metrics_service

        metrics_service.emit_metric("GuardrailInterventions", value=1)
        if intervention.get("blocked"):
            metrics_service.emit_metric("GuardrailBlocked", value=1)
    except Exception:  # noqa: BLE001 — never propagate a metrics failure
        logger.debug("guardrail metric emit failed", exc_info=True)


class GuardedBedrockClient:
    """Proxy around a bedrock-runtime client that applies the guardrail.

    Every attribute other than `converse` and `converse_stream` is delegated
    unchanged, so this is a drop-in replacement for the boto3 client.
    """

    def __init__(self, client: Any):
        self._client = client

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined on this class.
        return getattr(self._client, name)

    def converse(self, **kwargs: Any) -> Any:
        # An explicit guardrailConfig from the caller wins, so a call that needs
        # a different policy can still say so.
        if "guardrailConfig" not in kwargs:
            kwargs.update(guardrail_config())
        response = self._client.converse(**kwargs)
        self._record(response, kwargs.get("modelId", ""))
        return response

    def converse_stream(self, **kwargs: Any) -> Any:
        if "guardrailConfig" not in kwargs:
            kwargs.update(guardrail_config())
        # Streaming interventions arrive inside the event stream rather than on
        # the initial response, so they are accounted for by the consumer that
        # reads the events, not here.
        return self._client.converse_stream(**kwargs)

    def _record(self, response: Any, model_id: str) -> None:
        try:
            intervention = describe_intervention(response)
        except Exception:  # noqa: BLE001 — a parse failure must not fail the call
            logger.debug("guardrail trace parse failed", exc_info=True)
            return
        if not intervention:
            return
        level = logger.warning if intervention["blocked"] else logger.info
        level(
            "guardrail %s on %s (policies: %s)",
            intervention["action"],
            model_id or "unknown model",
            ", ".join(intervention["policies"]) or "none reported",
        )
        _emit_metrics(intervention)


def wrap(client: Any) -> Any:
    """Wrap a bedrock-runtime client so every Converse call carries the
    guardrail. Returns the client unchanged when no guardrail is configured, so
    an unconfigured environment pays no proxy overhead."""
    if not is_enabled():
        return client
    if isinstance(client, GuardedBedrockClient):
        return client
    return GuardedBedrockClient(client)
