# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AI-curated standards selection for a given document.

Given an SDD and the full list of HA standards, ask Bedrock to rank the
standards by applicability to the document and return the top N. Used
by the "AI-curated" mode of the Run Validation dialog so a user can say
"run me the 20 most relevant rules" without scrolling the full list.

This is intentionally a single Bedrock call, not an agent loop. The
prompt is small and the output is a JSON array of IDs — there is no tool
use, multi-turn reasoning, or hidden state worth the latency of a
ReAct loop.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Iterable, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from app.bedrock_models import OPUS_REASONING, model_for
from app.services.bedrock_guardrail import wrap as _wrap_guardrail

logger = logging.getLogger(__name__)

# Curate is reasoning-heavy: read the whole document, infer which
# library rules are applicable, and propose net-new rules to fill
# gaps. Worth Opus 4.7 even at higher per-call cost — it's a single
# call per curate request, not per chunk.
MODEL_ID = model_for("CURATOR_MODEL_ID", OPUS_REASONING)
DOCUMENT_TEXT_LIMIT = int(os.getenv("CURATOR_DOCUMENT_TEXT_LIMIT", "60000"))


def _make_bedrock_client():
    return _wrap_guardrail(boto3.client(
        "bedrock-runtime",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        config=Config(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        ),
    ))


def _build_document_text(document) -> str:
    parts: list[str] = []
    for s in document.sections:
        parts.append(f"[Page {s.page_number}] {s.title}\n{s.content}")
    full = "\n\n".join(parts)
    if len(full) > DOCUMENT_TEXT_LIMIT:
        full = full[:DOCUMENT_TEXT_LIMIT] + "\n\n[...document truncated...]"
    return full


def _summarize_standards(standards: Iterable[dict]) -> str:
    """Compact one-line representation per standard so the prompt stays small."""
    lines: list[str] = []
    for s in standards:
        sid = s.get("id", "")
        name = s.get("name", "")
        sev = s.get("severity", "")
        crit = s.get("validation_criteria", "")
        lines.append(f"- {sid} [{sev}] {name} :: {crit[:200]}")
    return "\n".join(lines)


_PROMPT_TEMPLATE = """You are an enterprise High Availability compliance auditor.

You will see a System Design Document and a list of HA standards.
Choose which standards to validate this document against.

You can do TWO things:

1. PICK existing standards from the library that are most applicable to
   this document — meaning standards whose criteria can be evaluated
   against this document with confidence (positively or negatively).

2. PROPOSE brand-new standards (up to {max_new}) when the user's intent
   or the document calls out HA requirements that NO existing rule
   captures. New rules are evaluated alongside picked ones for THIS
   validation only and the user can choose whether to save them to the
   library afterward.

{user_prompt_section}

# Document
{document_text}

# Existing standards (id [severity] name :: criteria)
{standards_summary}

# Output
Reply with **JSON only** — a single object of the form:

{{
  "existing_standard_ids": ["ID-1", "ID-2", ...],
  "new_suggestions": [
    {{
      "name": "Short title",
      "description": "1-2 sentences describing what the rule requires",
      "severity": "critical | high | medium | low | info",
      "category": "failover | recovery | networking | observability | replication | security | storage | identity | general",
      "validation_criteria": "Specific testable criteria an auditor checks against an SDD",
      "rationale": "1 sentence — why this rule is needed AND not covered by existing standards"
    }}
  ]
}}

Rules:
- existing_standard_ids must be exact ids from the library above (no inventing).
- existing_standard_ids ordered most-applicable first; total length ≤ {top_n}.
- Standards that don't apply to the technologies, tiers, or patterns
  in the document MUST be omitted, even if they have high severity in general.
- new_suggestions only when truly missing from the library — don't restate
  an existing rule with different wording.
- new_suggestions length ≤ {max_new}. Empty array is fine.
- No prose, no markdown fences.
"""


_USER_PROMPT_BLOCK = """# User's specific intent
The user described what they care about for this validation:

> {user_prompt}

Use this guidance to bias both selection and any new-rule suggestions.
"""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


_VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}
DEFAULT_MAX_NEW = 5


def _coerce_new_suggestion(raw: Any) -> Optional[dict]:
    """Validate a single new-rule suggestion. Drop if any required field is empty."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name", "")).strip()
    description = str(raw.get("description", "")).strip()
    criteria = str(raw.get("validation_criteria", "")).strip()
    if not (name and description and criteria):
        return None
    severity = str(raw.get("severity", "medium")).strip().lower()
    if severity not in _VALID_SEVERITIES:
        severity = "medium"
    category = str(raw.get("category", "general")).strip().lower() or "general"
    rationale = str(raw.get("rationale", "")).strip()
    return {
        "name": name,
        "description": description,
        "severity": severity,
        "category": category,
        "validation_criteria": criteria,
        "rationale": rationale,
    }


def _extract_curation(text: str) -> tuple[list[str], list[dict]]:
    """Return (existing_standard_ids, new_suggestions) from model output.

    Tolerates markdown fences and best-effort JSON extraction.
    """
    if not text:
        return [], []
    m = _JSON_FENCE_RE.search(text)
    candidate = m.group(1) if m else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return [], []
    try:
        parsed = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return [], []
    if not isinstance(parsed, dict):
        return [], []

    raw_ids = parsed.get("existing_standard_ids", []) or []
    if not isinstance(raw_ids, list):
        raw_ids = []
    existing = [str(x) for x in raw_ids if isinstance(x, (str, int))]

    raw_new = parsed.get("new_suggestions", []) or []
    if not isinstance(raw_new, list):
        raw_new = []
    new_suggestions: list[dict] = []
    for raw in raw_new:
        norm = _coerce_new_suggestion(raw)
        if norm:
            new_suggestions.append(norm)

    return existing, new_suggestions


def curate_standards(
    document,
    standards: list[dict],
    top_n: int,
    *,
    user_prompt: Optional[str] = None,
    max_new: int = DEFAULT_MAX_NEW,
) -> dict:
    """Curate standards for a document.

    Returns ``{"existing_standard_ids": [...], "new_suggestions": [...]}``.

    - ``existing_standard_ids`` is ranked, capped at ``top_n``, and only
      contains ids from ``standards``. Falls back to a severity-ordered
      slice if the model returns nothing usable.
    - ``new_suggestions`` is the model's set of brand-new rules — not
      saved anywhere; the caller decides what to do with them.
    - ``user_prompt`` injects natural-language guidance into the model
      prompt when provided.
    """
    if top_n <= 0 or not standards:
        return {"existing_standard_ids": [], "new_suggestions": []}

    document_text = _build_document_text(document)
    standards_summary = _summarize_standards(standards)
    user_prompt_section = (
        _USER_PROMPT_BLOCK.format(user_prompt=user_prompt.strip())
        if user_prompt and user_prompt.strip()
        else ""
    )
    prompt = _PROMPT_TEMPLATE.format(
        top_n=top_n,
        max_new=max_new,
        user_prompt_section=user_prompt_section,
        document_text=document_text,
        standards_summary=standards_summary,
    )

    bedrock = _make_bedrock_client()
    available_ids = {s.get("id") for s in standards if s.get("id")}

    try:
        response = bedrock.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            # temperature is deprecated on Opus 4.7 — passing it
            # produces a ConverseStream ValidationException.
            inferenceConfig={
                "maxTokens": 1500,
            },
        )
        content = response["output"]["message"]["content"]
        text = "".join(block.get("text", "") for block in content)
    except ClientError as e:
        logger.warning(f"Bedrock curation call failed: {e}")
        text = ""

    raw_existing, new_suggestions = _extract_curation(text)
    valid_existing = [sid for sid in raw_existing if sid in available_ids]

    if not valid_existing and not new_suggestions:
        # Fallback: severity-ordered slice. Better than returning nothing.
        severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        sorted_standards = sorted(
            standards,
            key=lambda s: (
                severity_rank.get(str(s.get("severity", "")).lower(), 9),
                s.get("id", ""),
            ),
        )
        valid_existing = [s.get("id", "") for s in sorted_standards if s.get("id")]

    return {
        "existing_standard_ids": valid_existing[:top_n],
        "new_suggestions": new_suggestions[:max_new],
    }
