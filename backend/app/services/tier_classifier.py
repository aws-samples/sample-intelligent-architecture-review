# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Classifies an SDD's service tier (1-5) using an LLM.

Why this lives in its own module
--------------------------------
The validator only runs rules that match the SDD's declared tier. The
SDD parsing pipeline doesn't actually know what tier the document
targets — that's a judgment call about the system's HA posture and
business criticality. So we ask Opus to read a slice of the parsed
SDD and pick a tier.

A failed classification is recoverable in the UI: the user can
override on the document detail page. Returning ``None`` from this
function (via ``classify_tier``) means "couldn't decide" and the
validator falls back to running all rules (universal default).
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

# Reasoning-heavy single call; worth Opus 4.7. Override via env if a
# customer's account doesn't have access.
MODEL_ID = model_for("TIER_CLASSIFIER_MODEL_ID", OPUS_REASONING)

# Cap how much of the SDD we send. Tier classification is a
# whole-document judgment so we want context, but Opus input is
# expensive — 30k chars is roughly the executive summary + first
# few architecture sections, which is what humans use too.
MAX_INPUT_CHARS = int(os.getenv("TIER_CLASSIFIER_MAX_CHARS", "30000"))

_PROMPT = """You are an enterprise architecture reviewer. Your job is to classify
a System Design Document (SDD) into a five-level resilience tier system.

The tier definitions, in order of decreasing criticality:

  Tier 1 — Mission-critical. Active-Active across isolated failure
           domains plus an independent recovery site. Automated
           failover and near-zero data loss. Examples: real-time
           transaction processing, identity, and emergency operations.
  Tier 2 — Business-critical. Redundant primary deployment plus a
           recovery site, synchronous replication where required,
           and automated local failover. Examples: high-volume
           customer-facing services and operational APIs.
  Tier 3 — Important. Redundancy in the primary environment plus a
           recovery path. Failover may require operator action.
           Examples: reporting and operational dashboards.
  Tier 4 — Standard internal or batch. Recoverable from a standby
           or backup within hours. Examples: batch processing and
           periodic data workflows.
  Tier 5 — Low criticality or non-production. Longer recovery targets
           are acceptable. Examples: internal utilities, development,
           and sandbox systems.

Read the SDD content below and reply with **JSON only**, matching:

{{
  "tier": 1 | 2 | 3 | 4 | 5,
  "confidence": "high" | "medium" | "low",
  "rationale": "<one or two sentences explaining why>"
}}

If the document explicitly states its tier (e.g. "Tier-0 critical"
or "Tier 4 batch"), respect that. Otherwise infer from:
  - Datacenter count and DR posture
  - Data replication mode (sync vs async, multi-master vs standby)
  - RTO / RPO numbers (Tier 1 ≤ 1 min RTO; Tier 4 measured in hours)
  - Stated business impact / customer-facing nature

Output JSON only — no markdown fences, no preamble.

# SDD content
{document_text}
"""


def _build_document_text(document) -> str:
    """Concatenate the SDD's parsed sections, capped at MAX_INPUT_CHARS."""
    parts: list[str] = []
    for s in getattr(document, "sections", []):
        # Page markers help the model locate sections it might cite,
        # matching the format used by the validator and curator.
        parts.append(f"[Page {s.page_number}] {s.title}\n{s.content}")
    full = "\n\n".join(parts).strip()
    if len(full) > MAX_INPUT_CHARS:
        full = full[:MAX_INPUT_CHARS] + "\n\n[...truncated for tier classification...]"
    return full


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


def classify_tier(document) -> Optional[int]:
    """Return the inferred tier (1..5) for the document, or None on failure.

    Failure modes (Bedrock unreachable, malformed JSON, out-of-range
    integer, empty document) all return None so the caller can decide
    how to surface "unknown" — typically by leaving declared_tier=None
    and letting the validator fall back to running all rules.
    """
    text = _build_document_text(document)
    if not text:
        logger.info(
            f"Tier classification skipped: no parsed text for {getattr(document, 'document_id', '?')}"
        )
        return None

    client = _wrap_guardrail(boto3.client(
        "bedrock-runtime",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        config=Config(
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        ),
    ))

    try:
        response = client.converse(
            modelId=MODEL_ID,
            messages=[
                {"role": "user", "content": [{"text": _PROMPT.format(document_text=text)}]}
            ],
            # temperature deprecated on Opus 4.7.
            inferenceConfig={"maxTokens": 400},
        )
        content = response["output"]["message"]["content"]
        raw = "".join(b.get("text", "") for b in content)
        parsed = _extract_json(raw)
        if not parsed:
            logger.warning(
                f"Tier classifier returned unparseable response for "
                f"{getattr(document, 'document_id', '?')}: {raw[:300]}"
            )
            return None

        tier_raw = parsed.get("tier")
        try:
            tier = int(tier_raw)
        except (TypeError, ValueError):
            logger.warning(
                f"Tier classifier returned non-integer tier {tier_raw!r} for "
                f"{getattr(document, 'document_id', '?')}"
            )
            return None
        if tier < 1 or tier > 5:
            logger.warning(
                f"Tier classifier returned out-of-range tier {tier} for "
                f"{getattr(document, 'document_id', '?')}"
            )
            return None

        confidence = parsed.get("confidence", "?")
        rationale = parsed.get("rationale", "")
        logger.info(
            f"Tier classification for {getattr(document, 'document_id', '?')}: "
            f"tier={tier} confidence={confidence} rationale={rationale!r}"
        )
        return tier

    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        logger.warning(
            f"Tier classifier Bedrock error ({code}) for "
            f"{getattr(document, 'document_id', '?')}: {e}"
        )
        return None
    except Exception:
        logger.exception(
            f"Tier classifier unexpected failure for "
            f"{getattr(document, 'document_id', '?')}"
        )
        return None
