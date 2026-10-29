# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Infers a document's hosting scope (on_prem | cloud | both) from its content.

Why this exists
---------------
``hosting_scope`` is declared by the user at upload time and then FORCED onto
every rule the extractor produces (`agents/extraction/context.py` deliberately
ignores the model's per-rule guess, to stop scope skew). That is the right
behavior when the declaration is correct — but it means a wrong selection is
silent: upload a cloud reference architecture as ``on_prem`` and all extracted
rules are tagged ``on_prem`` with nothing to signal the contradiction.

This classifier reads the document and infers what its content actually
describes, so the review gate can WARN when the inference contradicts the
declaration. It never blocks and never overrides: the reviewer decides. A
failure (Bedrock unreachable, unparseable reply, low confidence) returns None,
which the caller treats as "no opinion" — never as a mismatch.

Mirrors `tier_classifier`: one cheap Opus call, JSON-only reply, all failure
modes collapse to None.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.bedrock_models import OPUS_REASONING, model_for
from app.services.bedrock_guardrail import wrap as _wrap_guardrail

logger = logging.getLogger(__name__)

# Same reasoning tier as tier classification; overridable for accounts without
# Opus access.
MODEL_ID = model_for("SCOPE_CLASSIFIER_MODEL_ID", OPUS_REASONING)

# Scope is a whole-document judgment but the signal (where things run — owned
# datacenters vs a cloud provider's managed services) shows up early and
# repeatedly, so a generous slice of the front matter is plenty.
MAX_INPUT_CHARS = int(os.getenv("SCOPE_CLASSIFIER_MAX_CHARS", "30000"))

VALID_SCOPES = ("on_prem", "cloud", "both")

_PROMPT = """You are an enterprise infrastructure reviewer. Decide where the
system described by the document below is HOSTED, so its standards can be filed
under the right hosting scope.

Choose exactly one:
  on_prem — Runs in the organization's OWN facilities: named datacenter sites,
            owned hardware, SAN/fibre-channel storage, hardware load balancers or
            firewalls, colocation, "on-premises", raised-floor / rack language.
  cloud   — Runs on a public cloud provider's managed services: AWS/Azure/GCP,
            regions and availability zones, managed databases, object storage,
            autoscaling groups, serverless, managed Kubernetes.
  both    — Genuinely hybrid: substantial on-prem AND cloud components, or an
            explicit hybrid/migration architecture. Do NOT pick "both" merely
            because you are unsure — use low confidence for that.

Reply with JSON ONLY (no markdown fences, no preamble), matching:

{{
  "scope": "on_prem" | "cloud" | "both",
  "confidence": "high" | "medium" | "low",
  "rationale": "<one or two sentences naming the specific evidence>"
}}

Judge only from what the document states. If the hosting model is not
determinable from the content, pick your best guess and set confidence "low".

# Document content
{document_text}
"""

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = _JSON_FENCE_RE.search(text)
    candidate = m.group(1) if m else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None


def _client():
    return _wrap_guardrail(boto3.client(
        "bedrock-runtime",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        config=Config(
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        ),
    ))


def classify_scope(document_text: str) -> Optional[dict]:
    """Infer hosting scope from raw document text.

    Takes text rather than a document object so both the extraction pipeline
    (which works from Units) and the SDD pipeline (which has parsed sections)
    can call it without adapting a shape.

    Returns ``{"scope", "confidence", "rationale"}`` on success, or ``None`` on
    any failure — empty text, Bedrock error, unparseable reply, or a scope value
    outside the enum. None means "no opinion"; the caller must NOT read it as a
    mismatch.
    """
    text = (document_text or "").strip()
    if not text:
        logger.info("Scope classification skipped: empty document text")
        return None
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS] + "\n\n[...truncated for scope classification...]"

    try:
        response = _client().converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": _PROMPT.format(document_text=text)}]}],
            inferenceConfig={"maxTokens": 400},
        )
        content = response["output"]["message"]["content"]
        raw = "".join(b.get("text", "") for b in content)
        parsed = _extract_json(raw)
        if not parsed:
            logger.warning(f"Scope classifier returned unparseable response: {raw[:300]}")
            return None

        scope = str(parsed.get("scope", "")).strip().lower().replace("-", "_")
        if scope not in VALID_SCOPES:
            logger.warning(f"Scope classifier returned out-of-enum scope {scope!r}")
            return None

        confidence = str(parsed.get("confidence", "low")).strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        rationale = str(parsed.get("rationale", "") or "").strip()[:500]
        logger.info(f"Scope classification: scope={scope} confidence={confidence}")
        return {"scope": scope, "confidence": confidence, "rationale": rationale}

    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        logger.warning(f"Scope classifier Bedrock error ({code}): {e}")
        return None
    except Exception:
        logger.exception("Scope classifier unexpected failure")
        return None


def detect_scope_mismatch(declared_scope: Optional[str],
                          document_text: str,
                          *,
                          min_confidence: str = "medium") -> Optional[dict]:
    """Return a mismatch verdict when the inferred scope contradicts the
    declaration, else None.

    A mismatch is reported ONLY when:
      - the declaration is a concrete single scope (on_prem or cloud) — a
        document declared "both" accepts anything, so it can't be "wrong"; and
      - the inference is confident (>= min_confidence) — a low-confidence guess
        is not strong enough evidence to second-guess a human's selection; and
      - the inferred scope is a different concrete scope.

    An inferred "both" is NOT treated as contradicting a concrete declaration:
    "this looks hybrid" is weaker than "this is definitely cloud" and would
    produce noisy warnings on the common case of a doc that mentions both worlds
    in passing. This keeps the warning specific to the case the user described —
    a clearly-cloud document filed as on_prem, or vice versa.

    Returns ``{"declared", "inferred", "confidence", "rationale"}`` or None.
    """
    order = {"low": 0, "medium": 1, "high": 2}
    declared = (declared_scope or "").strip().lower().replace("-", "_")
    if declared not in ("on_prem", "cloud"):
        return None  # "both" (or unset) can't be contradicted

    verdict = classify_scope(document_text)
    if not verdict:
        return None
    inferred = verdict["scope"]
    if inferred not in ("on_prem", "cloud"):
        return None  # inferred "both" is too weak to flag a concrete declaration
    if inferred == declared:
        return None
    if order.get(verdict["confidence"], 0) < order.get(min_confidence, 1):
        return None

    return {
        "declared": declared,
        "inferred": inferred,
        "confidence": verdict["confidence"],
        "rationale": verdict["rationale"],
    }
