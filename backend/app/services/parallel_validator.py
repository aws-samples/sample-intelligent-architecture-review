# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Parallel per-standard, per-chunk validation against an SDD.

The previous version sent the *whole* document (truncated to 60k chars)
to a single Bedrock call per standard. That broke down on large SDDs:
anything past ~30 pages was silently dropped from the prompt, and the
user got a "compliant" verdict on rules whose evidence lived in chapters
that never reached the model.

This version splits the document into overlapping chunks and evaluates
every (standard × chunk) pair, then merges the chunk-level partials
into one finding per standard. The merge takes the most damning chunk's
verdict (FAIL > WARNING > PASS) so a single chunk reporting non-
compliance is not masked by other chunks where the topic simply wasn't
discussed.

Time budget
-----------
- Haiku 4.5 stays at ~1-3s per call.
- For an 80-rule × 5-chunk doc, that's 400 calls @ concurrency 25
  ≈ 16 batches ≈ 60-90s wall-clock.
- Small docs collapse to a single chunk so they keep the original
  ~30s budget.

Failure handling
----------------
A chunk-level call that exhausts retries becomes a WARNING partial.
If every chunk for a rule fails, the merged finding is WARNING with
the error chained in evidence. Other rules keep running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import boto3
from botocore.config import Config
from app.services.bedrock_guardrail import wrap as _wrap_guardrail
from botocore.exceptions import ClientError

from app.bedrock_models import SONNET, model_for
from app.services.prompt_safety import UNTRUSTED_DATA_NOTICE, untrusted_block
from app.models.validation import (
    ComplianceStatus,
    Finding,
    Severity,
    ValidationResult,
    ValidationStatus,
)

logger = logging.getLogger(__name__)


class ValidationCancelled(Exception):
    """Raised inside the validator when a run has been cancelled by the
    user, so the run unwinds and the caller can persist CANCELLED."""


# Cancellation is a PERSISTED signal, not process state.
#
# The API autoscales to several ECS tasks, so POST /{id}/cancel routinely lands
# on a task that isn't running the job. A process-local flag set there is read by
# no loop: the run completes and bills the full Bedrock fan-out while the UI
# reports it stopped. The flag therefore lives on the validation row, which every
# task can see.
#
# The in-process set stays as a same-task fast path, and reads are cached briefly
# because the checkpoints below run once per rule — a bare DDB read per rule would
# add hundreds of round-trips to a run.
_CANCELLED_VALIDATIONS: set[str] = set()

# How long a "not cancelled" answer may be reused. Bounds the delay between a
# user clicking Cancel and the run noticing, against read volume.
_CANCEL_CACHE_TTL_SEC = 3.0
_cancel_cache: dict[str, tuple[float, bool]] = {}


def request_cancel(validation_id: str) -> bool:
    """Mark a validation for cooperative cancellation.

    Returns whether the signal was durably recorded, so the route can avoid
    claiming a cancel landed when the write failed.
    """
    if not validation_id:
        return False
    _CANCELLED_VALIDATIONS.add(validation_id)
    # Invalidate any cached "not cancelled" answer in this process.
    _cancel_cache.pop(validation_id, None)
    from app.services.validation_store import validation_store
    return validation_store.request_cancel(validation_id)


def is_cancel_requested(validation_id: Optional[str]) -> bool:
    """True when the user asked to stop this run (from any task)."""
    if not validation_id:
        return False
    # Same-task cancel: no read needed, and it can never be stale.
    if validation_id in _CANCELLED_VALIDATIONS:
        return True

    import time as _time
    now = _time.monotonic()
    cached = _cancel_cache.get(validation_id)
    if cached and (now - cached[0]) < _CANCEL_CACHE_TTL_SEC:
        return cached[1]

    from app.services.validation_store import validation_store
    flag = validation_store.is_cancel_requested(validation_id)
    _cancel_cache[validation_id] = (now, flag)
    if flag:
        # Remember locally so the remaining checkpoints short-circuit.
        _CANCELLED_VALIDATIONS.add(validation_id)
    return flag


def clear_cancel(validation_id: Optional[str]) -> None:
    """Drop local cancellation state when a run ends, so neither the set nor the
    cache grows unbounded across many runs. The persisted flag stays on the row
    as a record of what happened."""
    if validation_id:
        _CANCELLED_VALIDATIONS.discard(validation_id)
        _cancel_cache.pop(validation_id, None)


MODEL_ID = model_for("VALIDATION_MODEL_ID", SONNET)
MAX_CONCURRENT = int(os.getenv("VALIDATION_MAX_CONCURRENT", "25"))
MAX_RETRIES = 3
INITIAL_BACKOFF_SEC = 1.0

# Adversarial verify pass. After a rule merges to FAIL,
# optionally run ONE more Bedrock call that tries to REFUTE the failure using
# the same evidence chunk. If the refutation is strong, the finding is
# downgraded FAIL -> WARNING (never silently -> PASS) and the challenge is
# recorded on the trace. Gated on + severity-scoped to bound cost: default ON
# but only for critical/high rules (the ones where a hallucinated FAIL is most
# damaging). Flip VALIDATION_VERIFY_FAILS=0 to disable entirely.
VERIFY_FAILS_ENABLED = os.getenv("VALIDATION_VERIFY_FAILS", "1") == "1"
VERIFY_FAILS_SEVERITIES = {
    s.strip().lower()
    for s in os.getenv("VALIDATION_VERIFY_SEVERITIES", "critical,high").split(",")
    if s.strip()
}
VERIFY_MAX_TOKENS = int(os.getenv("VALIDATION_VERIFY_MAX_TOKENS", "400"))

# Per-chunk character budget. Each chunk fits in a single prompt with
# room left for the standard, schema, and Haiku's output.
MAX_CHUNK_CHARS = int(os.getenv("VALIDATION_MAX_CHUNK_CHARS", "50000"))

# Sliding-window overlap so a fact split across a chunk boundary is
# still seen by at least one chunk in full.
CHUNK_OVERLAP_CHARS = int(os.getenv("VALIDATION_CHUNK_OVERLAP_CHARS", "5000"))

# Cap absurdly large docs at the API layer; this is the validator's
# own guardrail in case something slips past.
MAX_DOC_CHARS = int(os.getenv("VALIDATION_MAX_DOC_CHARS", "1_500_000"))

# Bedrock on-demand pricing for the validator model in USD per 1M
# tokens. Defaults match Sonnet 4.6 published list price; override
# via env if you switch models or hit a custom contract.
BEDROCK_INPUT_USD_PER_MTOK = float(os.getenv("VALIDATION_INPUT_USD_PER_MTOK", "3.0"))
BEDROCK_OUTPUT_USD_PER_MTOK = float(os.getenv("VALIDATION_OUTPUT_USD_PER_MTOK", "15.0"))


@dataclass
class _Chunk:
    """A slice of the document with the page range it covers."""
    index: int          # 0-based
    text: str
    page_start: int     # inclusive
    page_end: int       # inclusive


def _make_bedrock_client():
    """Bedrock runtime client tuned for many concurrent calls.

    Read timeout default 300s (was 60s). With Sonnet 4.6 multimodal
    + extended thinking, single Converse calls regularly
    take 60-180s and the old 60s cap timed them out before the
    response landed. The thinking budget is 8K tokens; the model
    has to render those before the answer block, which dominates
    latency on big docs. 300s is generous for the whole-doc path
    (~30 pages × 20 images) and harmless for chunked text.
    Override via BEDROCK_READ_TIMEOUT env var.
    """
    # Wrapped so every Converse call carries the guardrail. wrap() is a
    # pass-through when no guardrail is configured, so local runs are unchanged.
    return _wrap_guardrail(boto3.client(
        "bedrock-runtime",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        config=Config(
            retries={"max_attempts": 1, "mode": "standard"},
            connect_timeout=10,
            read_timeout=int(os.getenv("BEDROCK_READ_TIMEOUT", "300")),
            max_pool_connections=MAX_CONCURRENT * 2,
        ),
    ))


def _build_chunks(document) -> list[_Chunk]:
    """Slice the document into overlapping chunks with page metadata.

    Each section is concatenated with a `[Page N]` marker so we can
    recover page ranges by scanning markers in the chunk's text. The
    last chunk may be shorter; a doc smaller than MAX_CHUNK_CHARS
    collapses to one chunk.
    """
    parts: list[tuple[int, str]] = []  # (page_number, formatted_block)
    for s in document.sections:
        block = f"[Page {s.page_number}] {s.title}\n{s.content}"
        parts.append((s.page_number, block))

    full = "\n\n".join(b for _, b in parts)

    if len(full) > MAX_DOC_CHARS:
        logger.warning(
            f"Document {len(full)} chars exceeds hard cap {MAX_DOC_CHARS}; truncating."
        )
        full = full[:MAX_DOC_CHARS]

    if not full:
        return [_Chunk(index=0, text="", page_start=0, page_end=0)]

    # Single-chunk fast path
    if len(full) <= MAX_CHUNK_CHARS:
        return [_Chunk(
            index=0,
            text=full,
            page_start=parts[0][0] if parts else 0,
            page_end=parts[-1][0] if parts else 0,
        )]

    # Sliding window
    chunks: list[_Chunk] = []
    step = MAX_CHUNK_CHARS - CHUNK_OVERLAP_CHARS
    if step <= 0:
        step = MAX_CHUNK_CHARS  # defensive: misconfigured overlap

    page_re = re.compile(r"\[Page (\d+)\]")
    pos = 0
    idx = 0
    while pos < len(full):
        end = min(pos + MAX_CHUNK_CHARS, len(full))
        chunk_text = full[pos:end]
        page_nums = [int(m.group(1)) for m in page_re.finditer(chunk_text)]
        if page_nums:
            page_start, page_end = min(page_nums), max(page_nums)
        else:
            # No marker landed in this chunk (can happen mid-section);
            # fall back to the previous chunk's range.
            if chunks:
                page_start = page_end = chunks[-1].page_end
            else:
                page_start = page_end = 0
        chunks.append(_Chunk(index=idx, text=chunk_text, page_start=page_start, page_end=page_end))
        idx += 1
        if end >= len(full):
            break
        pos += step

    return chunks


_PROMPT_TEMPLATE = """You are an enterprise High Availability compliance auditor.

You are evaluating ONE chunk of a System Design Document (SDD) against ONE HA standard.
Other chunks are being evaluated separately and merged. Treat this chunk on its own
merits — only report what THIS chunk shows.

# Document chunk {chunk_idx} of {chunk_total} (covers pages {page_start}-{page_end})
{document_text}
{references_block}
# Standard to evaluate
ID: {std_id}
Title: {std_title}
Severity: {std_severity}
Description: {std_description}
Validation criteria: {std_criteria}{alternative_block}{source_quote_block}{rule_context_block}

# Your task
Reply with **JSON only**, no prose, matching this exact shape:

{{
  "status": "pass" | "fail" | "not_addressed" | "warning",
  "evidence": "<short quote or paraphrase from THIS chunk with page reference, OR a clear statement of what's missing>",
  "recommendation": "<what the document author should add or change; empty string if status is pass>",
  "affected_components": "<comma-separated component names from THIS chunk, or empty>",
  "source_section": "<section title and page where evidence appears, or empty>"
}}

Rules:
- "pass": clear evidence in THIS chunk that the standard IS satisfied — either the
  primary validation criteria OR any of the listed alternatives
- "fail": clear evidence in THIS chunk that the standard is NOT satisfied
- "not_addressed": this chunk does not discuss the topic at all (most chunks for most rules)
- "warning": chunk addresses the topic but evidence is partial or ambiguous
- Do NOT guess from context outside this chunk. If the chunk simply doesn't cover the topic, return "not_addressed".
- Output JSON only — no markdown fences, no explanations.
"""


def _source_quote_block(standard: dict) -> str:
    """Verbatim text of the standard from its source document, when the
    extractor captured it. This is the strongest grounding signal — it lets
    the model judge against the rule's ACTUAL wording, not just a paraphrase.
    Empty string when absent (hand-authored or older rules)."""
    sq = (standard.get("source_quote") or "").strip()
    if not sq:
        return ""
    return f"\nSource text (verbatim from the standards document): \"{sq[:800]}\""


def _rule_context_block(standard: dict, all_by_id: Optional[dict] = None) -> str:
    """Explain the rule's role in the founding/substitution/exception model
    so the model judges it correctly:

    - exception: it defines when the PARENT rule does NOT apply — a 'pass'
      means the SDD MEETS the exemption condition (the parent becomes N/A),
      NOT that the parent is satisfied.
    - substitution: it's an ALTERNATIVE that satisfies the parent; 'pass'
      means the SDD uses this alternative.
    Includes the parent's criteria for contrast when resolvable.

    Pre-fix, rule_type/parent linkage was never shown to the model on any
    path, so exceptions/substitutions were graded as if they were ordinary
    requirements. Empty string for plain founding rules (the common case).
    """
    rt = (standard.get("rule_type") or "founding").lower()
    if rt not in ("exception", "substitution"):
        return ""
    parent_id = standard.get("parent_rule_id")
    parent = (all_by_id or {}).get(parent_id) if parent_id else None
    parent_criteria = (parent or {}).get("validation_criteria", "") if parent else ""
    parent_line = (
        f"\n  Parent rule ({parent_id}) criteria: {parent_criteria}"
        if parent_criteria
        else (f"\n  Parent rule: {parent_id}" if parent_id else "")
    )
    if rt == "exception":
        return (
            "\n\n# Rule relationship — this is an EXCEPTION rule\n"
            "A 'pass' means the document MEETS this exemption condition, which "
            "makes the parent requirement NOT APPLICABLE. Judge whether the "
            "exemption condition holds — not whether the parent is satisfied."
            + parent_line
        )
    return (
        "\n\n# Rule relationship — this is a SUBSTITUTION rule\n"
        "A 'pass' means the document uses this ALTERNATIVE approach, which "
        "satisfies the parent requirement in place of the primary criteria."
        + parent_line
    )


def _build_prompt(
    chunk: _Chunk,
    total_chunks: int,
    standard: dict,
    references: Optional[list[dict]] = None,
    all_by_id: Optional[dict] = None,
) -> str:
    # alternative_criteria is optional — extracted by the tier-aware
    # extractor when the source doc lists substitution / alternative
    # architectures that satisfy the same rule. Surfacing them in the
    # prompt lets the validator pass an SDD that uses an alternative
    # without forcing it to mention the preferred architecture.
    alt = (standard.get("alternative_criteria") or "").strip()
    alternative_block = (
        f"\nAlternative architectures (any of these also pass): {alt}" if alt else ""
    )
    # References block. Each scope-matching Reference
    # contributes a markdown body the validator should treat as
    # factual context (e.g. v2 doc page 9's DC-peer table). Empty
    # string when no References attach — the prompt template still
    # places the block but it collapses cleanly.
    if references:
        # Reference text is attacker-influenceable and reaches the model on every
        # matching rule, so it is fenced rather than pasted as markdown headings:
        # otherwise a body could forge its own "## Standard to evaluate" section
        # and dictate a verdict.
        ref_lines = [
            "# References (factual context, NOT rules to validate against)",
            UNTRUSTED_DATA_NOTICE,
            "",
        ]
        for r in references:
            ref_lines.append(
                untrusted_block(r.get("body"), "reference",
                                name=r.get("name") or "(reference)")
            )
        references_block = "\n" + "\n".join(ref_lines)
    else:
        references_block = ""
    return _PROMPT_TEMPLATE.format(
        chunk_idx=chunk.index + 1,
        chunk_total=total_chunks,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        document_text=chunk.text,
        std_id=standard.get("id", ""),
        std_title=standard.get("name", ""),
        std_severity=standard.get("severity", "medium"),
        std_description=standard.get("description", ""),
        std_criteria=standard.get("validation_criteria", ""),
        alternative_block=alternative_block,
        source_quote_block=_source_quote_block(standard),
        rule_context_block=_rule_context_block(standard, all_by_id),
        references_block=references_block,
    )


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = _JSON_FENCE_RE.search(text)
    candidate = m.group(1) if m else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None


@dataclass
class _Partial:
    """One chunk's verdict on one standard, before merging."""
    chunk_index: int
    page_start: int
    page_end: int
    status: str  # raw model status: pass | fail | not_addressed | warning | error
    evidence: str
    recommendation: str
    affected: list[str]
    source_section: Optional[str]
    input_tokens: int = 0
    output_tokens: int = 0
    # Per-call trace data. Populated by
    # _evaluate_chunk_sync; merged into the winning chunk's Finding
    # so users can audit which prompt produced which verdict.
    prompt: str = ""
    raw_response: str = ""
    chunk_excerpt: str = ""
    latency_ms: int = 0
    # Verbalized reasoning from extended thinking. Empty
    # string when thinking was off or the model emitted no reasoning
    # block. Signature is opaque, needed only for tool-use
    # round-tripping (we store it but don't interpret it today).
    reasoning_text: str = ""
    reasoning_signature: Optional[str] = None


def _partial_from_response(
    chunk: _Chunk,
    parsed: Optional[dict],
    raw_text: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    prompt: str = "",
    latency_ms: int = 0,
    reasoning_text: str = "",
    reasoning_signature: Optional[str] = None,
) -> _Partial:
    # Capture chunk excerpt (first 600 chars) for the
    # trace so the UI can show "what the model saw" without storing
    # the full chunk text per-finding (chunks can be 50KB+).
    excerpt = (chunk.text or "")[:600]
    if parsed is None:
        return _Partial(
            chunk_index=chunk.index,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            status="error",
            evidence=f"Model response could not be parsed: {raw_text[:300]}",
            recommendation="",
            affected=[],
            source_section=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt=prompt,
            raw_response=raw_text[:2000],
            chunk_excerpt=excerpt,
            latency_ms=latency_ms,
            reasoning_text=reasoning_text,
            reasoning_signature=reasoning_signature,
        )
    raw_status = str(parsed.get("status", "not_addressed")).lower().strip()
    if raw_status not in ("pass", "fail", "not_addressed", "warning"):
        raw_status = "warning"
    affected = parsed.get("affected_components", "") or ""
    affected_list = (
        [c.strip() for c in affected.split(",") if c.strip()]
        if isinstance(affected, str)
        else []
    )
    return _Partial(
        chunk_index=chunk.index,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        status=raw_status,
        evidence=str(parsed.get("evidence", ""))[:1500],
        recommendation=str(parsed.get("recommendation", ""))[:800],
        affected=affected_list,
        source_section=parsed.get("source_section") or None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        prompt=prompt,
        raw_response=raw_text[:2000],
        chunk_excerpt=excerpt,
        latency_ms=latency_ms,
        reasoning_text=reasoning_text,
        reasoning_signature=reasoning_signature,
    )


def _format_page_range(page_start: int, page_end: int) -> str:
    """Render '(p.7)' for a single page or '(pp.7-12)' for a range."""
    if page_start == page_end:
        return f"p.{page_start}"
    return f"pp.{page_start}-{page_end}"


# A pass is only as good as the evidence behind it. When most chunks failed to
# process, the surviving "pass" covers a fraction of the document — so treating it
# as compliant reports a clean result for a run that mostly didn't happen.
#
# Tolerance: a single failed chunk out of many is noise (one throttled call on a
# 20-chunk document doesn't change the picture), but once failures reach a third
# of the chunks, or outnumber the passes, the verdict is no longer supported.
MAX_ERROR_FRACTION_FOR_PASS = 1 / 3


def _pass_is_undermined_by_errors(passes, errors, partials) -> bool:
    """True when a PASS rests on too little successfully-evaluated evidence."""
    if not errors:
        return False
    total = len(partials)
    if total == 0:
        return False
    # More chunks broke than passed, or a large share of the document is missing.
    return len(errors) > len(passes) or (len(errors) / total) > MAX_ERROR_FRACTION_FOR_PASS


def _merge_partials(standard: dict, partials: list[_Partial]) -> Finding:
    """Collapse all chunks' verdicts on a single standard into one finding.

    Precedence: FAIL > WARNING > PASS > not_addressed/error.
    Evidence comes from the highest-precedence chunk; if multiple chunks
    share the top tier, we take the lowest page number for stability.
    Affected components are unioned across chunks (preserving first-seen
    order).
    """
    severity_map = {
        "critical": Severity.CRITICAL,
        "high": Severity.HIGH,
        "medium": Severity.MEDIUM,
        "low": Severity.LOW,
        "info": Severity.INFO,
    }
    severity = severity_map.get(
        str(standard.get("severity", "medium")).lower(), Severity.MEDIUM
    )

    # Set when a verdict rests on too little evidence to trust (see
    # _pass_is_undermined_by_errors); prepended to the evidence so the reason is
    # visible in the report and the CSV/PDF export, not just in a log.
    degraded_note = ""

    # Bucket by status
    fails    = [p for p in partials if p.status == "fail"]
    warnings = [p for p in partials if p.status == "warning"]
    passes   = [p for p in partials if p.status == "pass"]
    errors   = [p for p in partials if p.status == "error"]
    # not_addressed chunks contribute nothing to verdict but their pages
    # still count as "scanned"

    # Pick verdict + the partial(s) whose evidence we'll surface.
    # Precedence FAIL > WARNING > PASS: a violation anywhere outranks a
    # pass elsewhere, and a warning (partial/ambiguous compliance) must
    # not be masked by a pass in another chunk. (Pre-fix code ordered
    # PASS above WARNING, contradicting the docstring and letting a
    # passing chunk hide a warning.)
    if fails:
        primary = sorted(fails, key=lambda p: p.page_start)[0]
        contributors = fails
        status = ComplianceStatus.FAIL
    elif warnings:
        primary = sorted(warnings, key=lambda p: p.page_start)[0]
        contributors = warnings
        status = ComplianceStatus.WARNING
    elif passes and not _pass_is_undermined_by_errors(passes, errors, partials):
        primary = sorted(passes, key=lambda p: p.page_start)[0]
        contributors = passes
        status = ComplianceStatus.PASS
    elif passes:
        # A pass we don't trust: most of the document couldn't be read (Bedrock
        # throttling, timeouts, unparseable responses), so "compliant" rests on a
        # fraction of the evidence. Reporting PASS here is the worst failure this
        # tool has — confidently wrong, and indistinguishable from a clean run.
        # Downgrade to WARNING so the rule shows up as unproven rather than met.
        primary = sorted(passes, key=lambda p: p.page_start)[0]
        contributors = passes
        status = ComplianceStatus.WARNING
        degraded_note = (
            f"Only {len(passes)} of {len(partials)} document sections could be "
            f"evaluated ({len(errors)} failed to process), so this cannot be "
            "confirmed as compliant. Re-run the validation."
        )
    elif errors and len(errors) == len(partials):
        # Every chunk errored — surface as warning with the first error
        primary = errors[0]
        contributors = errors
        status = ComplianceStatus.WARNING
    else:
        # No chunk addressed it AND no errors → NOT_ADDRESSED.
        # The document is silent on this rule. This is NOT a failure:
        # collapsing "not addressed" into FAIL (the pre-fix behavior)
        # made a doc that simply doesn't mention a rule read as
        # violating it, and drove overall scores to 0 on sparse
        # coverage. _calculate_score excludes NOT_ADDRESSED from the
        # denominator so it neither passes nor fails the document.
        # Even though no chunk produced a
        # verdict, we still ran a Bedrock call against each chunk and
        # got "not_addressed" responses back. Surface ONE of those as
        # the trace so the chat panel and trace drawer aren't empty:
        # the user sees the prompt we sent, the chunk we showed, and
        # the model's "I don't see this rule discussed" response,
        # which is exactly what they want to audit.
        absent_trace = None
        if partials:
            # Prefer a partial that actually has a prompt populated;
            # the very first partial is usually fine because
            # `_evaluate_chunk_sync` always records the prompt even
            # for not_addressed/error verdicts.
            traced = next(
                (p for p in partials if getattr(p, "prompt", None)),
                partials[0],
            )
            absent_trace = {
                "prompt": getattr(traced, "prompt", "") or "",
                "chunk_excerpt": getattr(traced, "chunk_excerpt", "") or "",
                "raw_response": getattr(traced, "raw_response", "") or "",
                "input_tokens": getattr(traced, "input_tokens", 0) or 0,
                "output_tokens": getattr(traced, "output_tokens", 0) or 0,
                "latency_ms": getattr(traced, "latency_ms", 0) or 0,
                # Include reasoning so
                # users can audit the model's "I scanned and didn't
                # find this" search, not just see an empty trace.
                "reasoning": getattr(traced, "reasoning_text", "") or None,
                "reasoning_signature": getattr(traced, "reasoning_signature", None),
                "model_id": MODEL_ID,
            } if getattr(traced, "prompt", None) else None
        return Finding(
            finding_id=f"find-{uuid.uuid4().hex[:8]}",
            requirement_id=standard.get("id", "UNKNOWN"),
            requirement_title=standard.get("name", "Unknown"),
            severity=severity,
            status=ComplianceStatus.NOT_ADDRESSED,
            evidence="The document does not address this standard.",
            recommendation=str(standard.get("description", ""))[:1000],
            affected_components=[],
            source_section=None,
            trace=absent_trace,
        )

    # Union affected components, first-seen order
    seen: set[str] = set()
    affected_union: list[str] = []
    for p in contributors:
        for c in p.affected:
            if c not in seen:
                seen.add(c)
                affected_union.append(c)

    # Collect additional page references where the same verdict
    # appeared, for the UI to render as separate chips.
    also_seen: list[str] = []
    if len(contributors) > 1:
        seen_ranges: set[tuple[int, int]] = {(primary.page_start, primary.page_end)}
        for p in contributors:
            key = (p.page_start, p.page_end)
            if key in seen_ranges:
                continue
            seen_ranges.add(key)
            also_seen.append(_format_page_range(p.page_start, p.page_end))

    # Assemble the trace from the winning chunk so
    # users can audit "what prompt produced this verdict?". Stored
    # inline on the Finding; ~3KB per finding on a typical run, well
    # within S3 limits. The trace also carries reasoning, model_id and
    # reasoning_signature when extended thinking captured them on the
    # winning chunk.
    trace = {
        "prompt": primary.prompt,
        "chunk_excerpt": primary.chunk_excerpt,
        "raw_response": primary.raw_response,
        "input_tokens": primary.input_tokens,
        "output_tokens": primary.output_tokens,
        "latency_ms": primary.latency_ms,
        "reasoning": getattr(primary, "reasoning_text", "") or None,
        "reasoning_signature": getattr(primary, "reasoning_signature", None),
        "model_id": MODEL_ID,
    } if primary.prompt else None

    return Finding(
        finding_id=f"find-{uuid.uuid4().hex[:8]}",
        requirement_id=standard.get("id", "UNKNOWN"),
        requirement_title=standard.get("name", "Unknown"),
        severity=severity,
        status=status,
        evidence=((degraded_note + " " if degraded_note else "")
                  + primary.evidence)[:2000],
        recommendation=primary.recommendation,
        affected_components=affected_union,
        source_section=primary.source_section,
        also_seen_pages=also_seen,
        trace=trace,
    )


def _evaluate_chunk_sync(
    bedrock_client,
    chunk: _Chunk,
    total_chunks: int,
    standard: dict,
    references: Optional[list[dict]] = None,
    all_by_id: Optional[dict] = None,
) -> _Partial:
    """One Bedrock call for (chunk × standard) with retry on throttle."""
    prompt = _build_prompt(chunk, total_chunks, standard, references, all_by_id)
    backoff = INITIAL_BACKOFF_SEC
    import time as _time
    # Extended thinking, gated by ENABLE_THINKING_CHUNKED
    # (default OFF — chunked path's call count makes the cost cliff
    # too steep for default-on). Operators flip via env var.
    from app.services.pdf_multimodal import (
        _thinking_enabled_chunked,
        _thinking_request_fields,
        _extract_reasoning_blocks,
        THINKING_BUDGET_TOKENS,
    )
    thinking_on = _thinking_enabled_chunked()
    inference_temp = 1.0 if thinking_on else 0.0
    # Bedrock requires maxTokens > budget_tokens when thinking is on.
    # Reserve ~1500 for the JSON answer beyond reasoning. When off,
    # the chunked path's existing 600-token cap is plenty.
    max_tokens = (THINKING_BUDGET_TOKENS + 1500) if thinking_on else 600
    extra_request_kwargs = _thinking_request_fields() if thinking_on else {}

    for attempt in range(MAX_RETRIES + 1):
        try:
            t0 = _time.monotonic()
            response = bedrock_client.converse(
                modelId=MODEL_ID,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": inference_temp,
                },
                **extra_request_kwargs,
            )
            latency_ms = int((_time.monotonic() - t0) * 1000)
            content = response["output"]["message"]["content"]
            text = "".join(block.get("text", "") for block in content)
            reasoning_text, reasoning_signature = _extract_reasoning_blocks(content)
            usage = response.get("usage") or {}
            return _partial_from_response(
                chunk,
                _extract_json(text),
                text,
                input_tokens=int(usage.get("inputTokens", 0)),
                output_tokens=int(usage.get("outputTokens", 0)),
                prompt=prompt,
                latency_ms=latency_ms,
                reasoning_text=reasoning_text,
                reasoning_signature=reasoning_signature,
            )

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ThrottlingException" and attempt < MAX_RETRIES:
                import time as _time
                _time.sleep(backoff)  # nosemgrep: arbitrary-sleep — intentional exponential backoff on Bedrock ThrottlingException
                backoff *= 2
                continue
            logger.warning(
                f"Bedrock error chunk={chunk.index} std={standard.get('id')}: {code}: {e}"
            )
            return _partial_from_response(
                chunk, None, f"Bedrock error after {attempt} retries: {code}"
            )
        except Exception as e:
            logger.exception(
                f"Unexpected error chunk={chunk.index} std={standard.get('id')}"
            )
            return _partial_from_response(chunk, None, f"Unexpected error: {e}")

    return _partial_from_response(chunk, None, "Exhausted retries")


def _should_verify(finding: "Finding") -> bool:
    """Verify only FAILs on the configured severities, when enabled."""
    if not VERIFY_FAILS_ENABLED:
        return False
    if finding.status != ComplianceStatus.FAIL:
        return False
    sev = finding.severity.value if hasattr(finding.severity, "value") else str(finding.severity)
    return sev.lower() in VERIFY_FAILS_SEVERITIES


def _verify_refuted(verdict: Optional[dict]) -> bool:
    """True when a verify verdict overturns a FAIL: refuted (bool True or the
    string 'true') AND confidence high/medium. Lenient on the model returning
    a stringified boolean; strict on confidence."""
    if not verdict:
        return False
    refuted = verdict.get("refuted")
    refuted_ok = refuted is True or str(refuted).strip().lower() == "true"
    conf_ok = str(verdict.get("confidence", "")).strip().lower() in ("high", "medium")
    return refuted_ok and conf_ok


def _verify_fail_sync(
    bedrock_client, standard: dict, finding: "Finding"
) -> tuple[Optional[dict], int, int]:
    """Adversarial second opinion on a FAIL. Ask the model to REFUTE the
    failure using the evidence the first pass cited. Returns
    (verdict_or_None, input_tokens, output_tokens) — the token counts feed the
    run's cost telemetry so the verify call isn't billed-but-invisible.

    Deliberately biased toward CONFIRMING the fail (default refuted=false):
    we only overturn a failure on clear contrary evidence, so the pass
    reduces false FAILs without manufacturing false PASSes.
    """
    evidence = (finding.evidence or "")[:1500]
    trace = finding.trace or {}
    chunk_excerpt = (trace.get("chunk_excerpt") or "")[:2000]
    prompt = (
        "You are a skeptical reviewer auditing a compliance AUDITOR's verdict. "
        "The auditor marked a rule as FAILED. Your job is to challenge that: "
        "using ONLY the document evidence below, decide whether the failure is "
        "actually justified, or whether the auditor missed evidence that the "
        "rule IS satisfied.\n\n"
        f"# Rule\nID: {standard.get('id','')}\nTitle: {standard.get('name','')}\n"
        f"Criteria: {standard.get('validation_criteria','')}\n\n"
        f"# Auditor's FAIL evidence\n{evidence}\n\n"
        f"# Document excerpt the auditor saw\n{chunk_excerpt}\n\n"
        "Return JSON ONLY:\n"
        '{"refuted": true|false, "confidence": "high"|"medium"|"low", '
        '"rationale": "<one sentence>"}\n'
        "Set refuted=true ONLY if the evidence clearly shows the rule IS "
        "satisfied (the FAIL was wrong). If the failure looks justified or the "
        "evidence is ambiguous, refuted=false. Default to false when unsure."
    )
    try:
        response = bedrock_client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={
                "maxTokens": VERIFY_MAX_TOKENS,
                "temperature": 0.0,
            },
        )
        text = "".join(b.get("text", "") for b in response["output"]["message"]["content"])
        usage = response.get("usage") or {}
        return (
            _extract_json(text),
            int(usage.get("inputTokens", 0)),
            int(usage.get("outputTokens", 0)),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"verify-fail pass errored for {standard.get('id')}: {e}")
        return (None, 0, 0)


async def _verify_findings_async(
    bedrock_client,
    findings: list["Finding"],
    std_by_id: dict,
    sem: asyncio.Semaphore,
) -> tuple[int, int, int]:
    """Run the adversarial verify pass over every eligible finding, in
    parallel, off the event loop. Returns (calls, input_tokens, output_tokens)
    for the run's cost telemetry.

    Each verify is a blocking Bedrock call, and the multimodal and PPTX
    validators run on the API's own event loop. Calling _verify_fail_sync
    inline there serialized one model call per FAIL and froze the loop for the
    whole pass: with a few hundred rules that is minutes, the ALB health check
    times out, ECS replaces the task, and every in-flight validation dies.
    to_thread keeps the loop responsive; the shared semaphore keeps Bedrock
    concurrency within the run's limit.
    """
    targets = [f for f in findings if _should_verify(f)]

    async def _one(f: "Finding") -> tuple[int, int]:
        async with sem:
            verdict, v_in, v_out = await asyncio.to_thread(
                _verify_fail_sync, bedrock_client, std_by_id.get(f.requirement_id, {}), f
            )
        _apply_verify_verdict(f, verdict)
        return v_in, v_out

    usage = await asyncio.gather(*(_one(f) for f in targets))
    return len(targets), sum(u[0] for u in usage), sum(u[1] for u in usage)


def _apply_verify_verdict(finding: "Finding", verdict: Optional[dict]) -> None:
    """Downgrade a FAIL to WARNING in place when the verify pass refuted it,
    recording the challenge. Records the challenge even when trace is None (by
    creating a minimal trace) so the audit trail never loses it."""
    if not _verify_refuted(verdict):
        return
    finding.status = ComplianceStatus.WARNING
    note = str(verdict.get("rationale", ""))[:300]
    finding.evidence = (
        f"[Verify pass challenged the FAIL — downgraded to WARNING] "
        f"{note}\n\nOriginal: {finding.evidence}"
    )[:2000]
    if not isinstance(finding.trace, dict):
        finding.trace = {}
    finding.trace["verify_challenge"] = verdict


async def _evaluate_chunk(
    bedrock_client,
    chunk: _Chunk,
    total_chunks: int,
    standard: dict,
    sem: asyncio.Semaphore,
    references: Optional[list[dict]] = None,
    all_by_id: Optional[dict] = None,
) -> _Partial:
    async with sem:
        return await asyncio.to_thread(
            _evaluate_chunk_sync,
            bedrock_client,
            chunk,
            total_chunks,
            standard,
            references,
            all_by_id,
        )


async def validate_document(
    document,
    standards: list[dict],
    *,
    document_content_hash: Optional[str] = None,
    standards_set_hash: Optional[str] = None,
    standards_set: str = "default",
    validation_id: Optional[str] = None,
    on_progress: Optional[Any] = None,
    # Pre-validation tier override. When None, the
    # validator uses document.declared_tier. When set, every rule
    # filter pretends the SDD is at this tier instead.
    tier_override: Optional[int] = None,
) -> ValidationResult:
    """Run chunked, parallel per-(standard, chunk) validation.

    When document.source_format == PPTX, dispatch to the
    per-slide validator instead. The per-slide path batches rules
    (default 10 per multimodal call) so a 30-slide × 100-rule
    validation issues 30 × 10 = 300 calls, not 30 × 100 = 3000.

    on_progress, if provided, is called with `(rules_completed, total_rules)`
    each time *all* chunks for a rule have completed and the rule's
    finding has been merged. The progress bar still tracks rules, not
    chunks, so the UX matches the old behavior.
    """
    # Dispatch: PPTX docs use slide-mode validation.
    from app.models.document import SourceFormat
    if getattr(document, "source_format", SourceFormat.PDF) == SourceFormat.PPTX:
        return await validate_pptx_document(
            document=document,
            standards=standards,
            document_content_hash=document_content_hash,
            standards_set_hash=standards_set_hash,
            standards_set=standards_set,
            validation_id=validation_id,
            on_progress=on_progress,
        )

    # Dispatch: diagram-bearing PDFs use the multimodal
    # validator so the model actually sees architecture diagrams. We
    # short-circuit before the text-only path: fetch the original PDF
    # bytes from S3, check for embedded images, and route accordingly.
    # Pure-text PDFs (no embedded images detected) keep the cheaper
    # text-only flow that follows below.
    try:
        from app.services.document_store import document_store
        from app.utils.pdf_parser import has_diagram_pages

        s3_key = getattr(document, "s3_key", None)
        if s3_key:
            pdf_bytes_for_route = document_store.download_document(s3_key)
            if pdf_bytes_for_route and has_diagram_pages(pdf_bytes_for_route):
                logger.info(
                    f"Routing PDF {document.document_id} to multimodal "
                    f"validator (has embedded diagrams)"
                )
                # Build per-page text from the parsed sections, grouping
                # by page_number. The multimodal validator pairs each
                # page's text with its rendered PNG.
                pages_by_num: dict[int, list[str]] = {}
                for sec in (document.sections or []):
                    pages_by_num.setdefault(sec.page_number, []).append(
                        f"{sec.title}\n{sec.content}"
                    )
                if pages_by_num:
                    last_page = max(pages_by_num.keys())
                    page_texts = [
                        "\n\n".join(pages_by_num.get(p, []))
                        for p in range(1, last_page + 1)
                    ]
                else:
                    page_texts = []

                # The multimodal validator is async-native;
                # call it directly so as_completed progress + Bedrock
                # concurrency stay on the route's event loop.
                from app.services.pdf_multimodal import (
                    _validate_pdf_multimodal_async,
                )
                effective_tier = (
                    tier_override
                    if tier_override is not None
                    else getattr(document, "declared_tier", None)
                )
                result = await _validate_pdf_multimodal_async(
                    pdf_bytes=pdf_bytes_for_route,
                    page_texts=page_texts,
                    document_id=document.document_id,
                    standards=standards,
                    declared_tier=effective_tier,
                    declared_scope=getattr(document, "hosting_scope", None),
                    standards_set=standards_set,
                    on_progress=on_progress,
                    # Thread the real validation_id so cooperative
                    # cancellation checks match the id the cancel endpoint
                    # registers.
                    validation_id=validation_id,
                )
                # Preserve the supplied validation_id so the caller's
                # background-task plumbing keeps working.
                if validation_id:
                    result.validation_id = validation_id
                # Provenance fields the multimodal path doesn't emit itself.
                result.tier_used = effective_tier
                result.tier_override = tier_override
                result.document_content_hash = document_content_hash
                result.standards_set_hash = standards_set_hash
                # The decision tree +
                # layer/architecture rollups + score are applied INSIDE
                # _validate_pdf_multimodal_async over the tier/scope-filtered
                # standards it actually evaluated (running them here over the
                # unfiltered superset produced wrong verdicts). Nothing more
                # to resolve — just return.
                return result
    except ValidationCancelled:
        # User cancellation must NOT be swallowed by the broad handler
        # below (which would emit a false "route failed" alarm AND fall
        # through to a full text-only re-run). Propagate so _run_validation
        # persists CANCELLED.
        raise
    except Exception as exc:  # noqa: BLE001
        # Defensive: any OTHER failure during multimodal routing falls
        # through to the text-only path below. We log loudly so the
        # regression is obvious in CloudWatch.
        logger.exception(
            f"Multimodal route failed for {document.document_id}; "
            f"falling back to text-only: {exc}"
        )
    started_at = datetime.utcnow()
    validation_id = validation_id or f"val-{uuid.uuid4().hex[:12]}"
    total_provided = len(standards)

    if not standards:
        raise ValueError("No standards provided for validation")

    # Tier filtering. The SDD declares a tier (1..5); a rule applies
    # only if its applies_to_tiers contains that tier. When the
    # document has no declared_tier (older or unknown), we run every
    # rule — same behavior as before tier-awareness landed. When a
    # rule has no applies_to_tiers, default to all tiers (universal).
    # tier_override from the validation request
    # takes precedence over declared_tier when set. validate_document
    # accepts override via the tier_override kwarg below; the route
    # plumbs it from ValidationRequest.tier_override.
    declared_tier = getattr(document, "declared_tier", None)
    effective_tier = tier_override if tier_override is not None else declared_tier
    skipped_tier = 0
    if effective_tier is not None:
        # Reassign declared_tier alias for the existing block below
        # so the rest of the function stays unchanged.
        declared_tier = effective_tier
    if effective_tier is not None:
        applicable: list[dict] = []
        for s in standards:
            tiers = s.get("applies_to_tiers")
            if not tiers:
                # No tier scope on the rule → universal → applies.
                applicable.append(s)
                continue
            try:
                if int(declared_tier) in [int(t) for t in tiers]:
                    applicable.append(s)
                else:
                    skipped_tier += 1
            except (TypeError, ValueError):
                # Malformed tiers — be permissive, include the rule.
                applicable.append(s)
        standards = applicable

    # hosting_scope filter. Composes with the tier filter
    # via AND. A rule applies if rule.hosting_scope ∈
    # {document.hosting_scope, "both"}. Both fields are required by
    # the schema, so no null branches. The schema-level required
    # field also means rule.hosting_scope is always a real value;
    # if a rule somehow lacks it (e.g. a malformed manual-insert
    # bypassing Pydantic), we surface via direct dict access rather
    # than silent default.
    declared_scope = document.hosting_scope
    skipped_scope = 0
    scoped: list[dict] = []
    for s in standards:
        rule_scope = s["hosting_scope"]
        if rule_scope == declared_scope or rule_scope == "both":
            scoped.append(s)
        else:
            skipped_scope += 1
    standards = scoped

    skipped = skipped_tier + skipped_scope
    total = len(standards)

    if total == 0:
        # Every rule got filtered out. Score is None (not-applicable),
        # NOT 100 — a doc that matched zero rules is not "100%
        # compliant"; reporting 100 here read as a perfect pass and was
        # a top validation bug. See _calculate_score.
        logger.info(
            f"Validation {validation_id}: 0 applicable standards after "
            f"filtering (tier={declared_tier}, scope={declared_scope}, "
            f"provided={total_provided}, skipped_tier={skipped_tier}, "
            f"skipped_scope={skipped_scope})"
        )
        return ValidationResult(
            validation_id=validation_id,
            document_id=document.document_id,
            status=ValidationStatus.COMPLETED,
            standards_set=standards_set,
            started_at=started_at,
            completed_at=datetime.utcnow(),
            findings=[],
            overall_score=None,
            summary=(
                f"Not applicable — no standards apply to Tier "
                f"{declared_tier} / scope {declared_scope} "
                f"(of {total_provided} total)."
            ),
            document_content_hash=document_content_hash,
            standards_set_hash=standards_set_hash,
            evaluated_count=0,
            total_standards=0,
            chunks_per_document=0,
            bedrock_calls=0,
            bedrock_input_tokens=0,
            bedrock_output_tokens=0,
            estimated_cost_usd=0.0,
            skipped_count=skipped,
            skipped_count_breakdown={"tier": skipped_tier, "scope": skipped_scope},
        )

    chunks = _build_chunks(document)
    bedrock = _make_bedrock_client()
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    total_chars = sum(len(c.text) for c in chunks)
    # Log the exact rule-set (count + hash) + tier so two runs of the
    # SAME document that scope to different rule counts are explainable
    # (e.g. the library changed between runs, or a different tier).
    logger.info(
        f"Starting chunked validation {validation_id}: "
        f"{total} standards × {len(chunks)} chunks = "
        f"{total * len(chunks)} calls; concurrency={MAX_CONCURRENT}, "
        f"doc={document.document_id} (tier={declared_tier}, "
        f"scope={declared_scope}, skipped={skipped}, "
        f"standards_set_hash={standards_set_hash}, {total_chars} chars)"
    )

    # Fetch References once and slice per rule by
    # scope match. Cheap (one DDB scan per validation) and avoids
    # re-fetching for every chunk.
    try:
        from app.services.references_store import references_store
        all_refs = references_store.list_all(status="active")
    except Exception as exc:
        logger.warning(f"references_store unavailable; running without refs: {exc}")
        all_refs = []

    def _refs_for(std: dict) -> list[dict]:
        if not all_refs:
            return []
        rule_tiers = {int(t) for t in (std.get("applies_to_tiers") or [])}
        rule_layer = std.get("layer")
        rule_scope = std.get("hosting_scope") or "both"
        out = []
        for ref in all_refs:
            ref_tiers = {int(t) for t in (ref.get("applies_to_tiers") or [])}
            if rule_tiers and ref_tiers and not (rule_tiers & ref_tiers):
                continue
            ref_layer = ref.get("layer")
            if rule_layer and ref_layer and ref_layer != rule_layer:
                continue
            ref_scope = ref.get("hosting_scope") or "both"
            if ref_scope != "both" and ref_scope != rule_scope:
                continue
            out.append(ref)
        return out

    # One async task per standard. Inside each task we fan out across
    # chunks and merge — that way `as_completed` over the standard-level
    # tasks gives us natural rule-level progress for the UI. We return
    # the partials alongside the finding so the outer aggregator can
    # tally tokens without a second pass.
    # Index by id so exception/substitution rules can show their parent's
    # criteria in the prompt (rule-relationship context).
    all_by_id = {s["id"]: s for s in standards if s.get("id")}

    async def evaluate_one_standard(
        std: dict,
    ) -> tuple[Finding, list[_Partial], int, int, int]:
        """Returns (finding, partials, verify_calls, verify_in_tok,
        verify_out_tok) so the outer loop can fold the verify pass's Bedrock
        usage into the run's cost telemetry."""
        scoped_refs = _refs_for(std)
        chunk_tasks = [
            _evaluate_chunk(bedrock, c, len(chunks), std, sem, scoped_refs, all_by_id)
            for c in chunks
        ]
        partials = await asyncio.gather(*chunk_tasks)
        finding = _merge_partials(std, partials)
        v_calls = v_in = v_out = 0
        # Adversarial verify pass — challenge FAILs on high-stakes rules.
        if _should_verify(finding):
            async with sem:
                verdict, v_in, v_out = await asyncio.to_thread(
                    _verify_fail_sync, bedrock, std, finding
                )
            v_calls = 1
            # Overturn to WARNING (never PASS) when refuted with confidence;
            # records the challenge (even if trace was None).
            _apply_verify_verdict(finding, verdict)
        return finding, partials, v_calls, v_in, v_out

    rule_tasks = [asyncio.create_task(evaluate_one_standard(s)) for s in standards]
    findings: list[Finding] = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_calls = 0
    for coro in asyncio.as_completed(rule_tasks):
        # Cooperative cancellation checkpoint: between rule completions,
        # if the user cancelled, stop launching more work, cancel the
        # in-flight rule tasks, and unwind so the caller persists
        # CANCELLED. Checked here (not mid-Bedrock-call) so a cancel takes
        # effect within one rule's latency.
        if is_cancel_requested(validation_id):
            for t in rule_tasks:
                t.cancel()
            # Await the cancelled tasks so they unwind cleanly (no
            # "Task was destroyed but it is pending" / unretrieved-exception
            # warnings) before we raise.
            await asyncio.gather(*rule_tasks, return_exceptions=True)
            logger.info(f"Validation {validation_id} cancelled by user")
            raise ValidationCancelled(validation_id)
        finding, partials, v_calls, v_in, v_out = await coro
        findings.append(finding)
        total_calls += len(partials) + v_calls
        for p in partials:
            total_input_tokens += p.input_tokens
            total_output_tokens += p.output_tokens
        # Fold the verify pass's Bedrock usage into the run totals so cost
        # telemetry + the CloudWatch metric aren't under-reported.
        total_input_tokens += v_in
        total_output_tokens += v_out
        if on_progress is not None:
            try:
                maybe_coro = on_progress(len(findings), total)
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception:
                logger.exception("Progress callback raised; ignoring")

    # Post-loop cancellation check — honour a cancel that arrived during the
    # final in-flight rule call (which can't be interrupted mid-flight) by
    # discarding rather than persisting a completed run.
    if is_cancel_requested(validation_id):
        logger.info(f"Validation {validation_id} cancelled by user (post-loop)")
        raise ValidationCancelled(validation_id)

    # Decision tree + architecture rollup (shared across all
    # validation paths via resolve_decision_tree).
    findings, layer_verdicts, architecture_verdict = resolve_decision_tree(
        findings,
        standards,  # the post-filter list (already tier+scope-filtered)
        tier_used=effective_tier,
    )

    overall_score = _calculate_score(findings)
    completed_at = datetime.utcnow()
    elapsed = (completed_at - started_at).total_seconds()
    estimated_cost = round(
        (total_input_tokens / 1_000_000) * BEDROCK_INPUT_USD_PER_MTOK
        + (total_output_tokens / 1_000_000) * BEDROCK_OUTPUT_USD_PER_MTOK,
        4,
    )
    logger.info(
        f"Completed validation {validation_id} in {elapsed:.1f}s: "
        f"score={overall_score}, findings={len(findings)}, "
        f"chunks={len(chunks)}, calls={total_calls}, "
        f"in_tokens={total_input_tokens}, out_tokens={total_output_tokens}, "
        f"cost=${estimated_cost:.4f}"
    )

    # Emit telemetry to CloudWatch for the dashboard. Failures here
    # must not break the validation; metrics are best-effort.
    try:
        from app.services.metrics import metrics_service
        metrics_service.validation_cost(
            document_id=document.document_id,
            validation_id=validation_id,
            calls=total_calls,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            estimated_cost_usd=estimated_cost,
            chunks=len(chunks),
            duration_seconds=elapsed,
        )
    except Exception:
        logger.exception("Failed to emit validation cost metrics; continuing")

    chunk_summary = (
        f" across {len(chunks)} chunk{'s' if len(chunks) != 1 else ''}"
        if len(chunks) > 1
        else ""
    )
    return ValidationResult(
        validation_id=validation_id,
        document_id=document.document_id,
        status=ValidationStatus.COMPLETED,
        standards_set=standards_set,
        started_at=started_at,
        completed_at=completed_at,
        findings=list(findings),
        overall_score=overall_score,
        summary=f"Evaluated {len(findings)} standards{chunk_summary} in {elapsed:.1f}s.",
        document_content_hash=document_content_hash,
        standards_set_hash=standards_set_hash,
        evaluated_count=len(findings),
        total_standards=total,
        chunks_per_document=len(chunks),
        bedrock_calls=total_calls,
        bedrock_input_tokens=total_input_tokens,
        bedrock_output_tokens=total_output_tokens,
        estimated_cost_usd=estimated_cost,
        skipped_count=skipped,
        skipped_count_breakdown={"tier": skipped_tier, "scope": skipped_scope},
        # Decision-tree rollup + tier provenance.
        layer_verdicts=layer_verdicts,
        architecture_verdict=architecture_verdict,
        tier_used=effective_tier,
        tier_override=tier_override,
    )


def _apply_decision_tree(
    findings: list[Finding],
    standards: list[dict],
    tier_used: Optional[int],
) -> tuple[list[Finding], list]:
    """Apply the per-(tier × layer) decision tree.

    Inputs:
      findings: raw finding-per-rule output from the parallel fan-out.
        Statuses are PASS / FAIL / WARNING based on each rule's
        independent evaluation.
      standards: the same rule dicts that produced findings (we need
        rule_type, exception_scope, parent_rule_id metadata that the
        finding object alone doesn't carry).
      tier_used: the tier we filtered to. None when the SDD has no
        declared tier and no override; we treat the whole library as
        one tier-bucket in that case.

    Returns (mutated_findings, layer_verdicts):
      mutated_findings: same findings list with status / via_rule_id /
        tried_substitution_ids / rule_type fields updated to reflect
        the decision-tree resolution. Substitutions and exceptions
        keep their raw verdicts but get rule_type tagged so the UI
        can group them.
      layer_verdicts: one LayerVerdictRollup per (tier × layer) pair
        the validator considered.

    Algorithm:
      for each (tier, layer):
        if any layer_exception passed → layer N/A, all founding rules
          and substitutions in that layer get
          NOT_APPLICABLE_VIA_EXCEPTION with via_rule_id pointing at
          the exception. Founding-rule subtitutions don't fire.
        else for each founding rule:
          if a rule_exception passed → finding becomes
            NOT_APPLICABLE_VIA_EXCEPTION (via_rule_id = exception.id)
          elif founding finding is PASS → keep as PASS, rule_type=founding
          else → check substitutions of this founding rule:
            if any substitution PASSED → finding becomes
              PASS_VIA_SUBSTITUTION with via_rule_id = substitution.id
            else → keep as FAIL, but record tried_substitution_ids
        layer rollup = AND of founding-rule verdicts after rescue.
    """
    from app.models.validation import LayerVerdict, LayerVerdictRollup

    finding_by_id: dict[str, Finding] = {f.requirement_id: f for f in findings}

    # Group rule ids by (tier, layer). A rule's tier set may include
    # multiple tiers (e.g. universal rule with [1..5]); we still slot
    # it under tier_used only because that's what we filtered to.
    layer_groups: dict[tuple[int, str], dict] = {}
    for s in standards:
        layer = s.get("layer") or "general"
        # tier_used is what we ran against; bucket everything there.
        # When tier_used is None (no declared tier), use sentinel 0.
        bucket_tier = tier_used if tier_used is not None else 0
        key = (bucket_tier, layer)
        group = layer_groups.setdefault(
            key,
            {
                "founding": [],
                "substitution_by_parent": {},
                "rule_exception_by_parent": {},
                "layer_exceptions": [],
            },
        )
        rule_type = s.get("rule_type") or "founding"
        if rule_type == "founding":
            group["founding"].append(s["id"])
        elif rule_type == "substitution":
            parent = s.get("parent_rule_id")
            if parent:
                group["substitution_by_parent"].setdefault(parent, []).append(s["id"])
        elif rule_type == "exception":
            scope = s.get("exception_scope") or (
                "rule" if s.get("parent_rule_id") else "layer"
            )
            if scope == "layer":
                group["layer_exceptions"].append(s["id"])
            else:
                parent = s.get("parent_rule_id")
                if parent:
                    group["rule_exception_by_parent"].setdefault(parent, []).append(s["id"])

    layer_verdicts: list = []

    for (tier, layer), group in layer_groups.items():
        # Layer exception short-circuit.
        firing_layer_exception: Optional[str] = None
        for ex_id in group["layer_exceptions"]:
            f = finding_by_id.get(ex_id)
            if f and f.status == ComplianceStatus.PASS:
                firing_layer_exception = ex_id
                break

        if firing_layer_exception:
            # Mark every founding finding in this layer as
            # not-applicable-via-exception. Substitutions don't fire.
            for fid in group["founding"]:
                f = finding_by_id.get(fid)
                if f is None:
                    continue
                f.status = ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION
                f.via_rule_id = firing_layer_exception
                f.rule_type = "founding"
            # Tag the exception itself.
            ef = finding_by_id.get(firing_layer_exception)
            if ef:
                ef.rule_type = "exception"
            layer_verdicts.append(
                LayerVerdictRollup(
                    tier=tier,
                    layer=layer,
                    verdict=LayerVerdict.NOT_APPLICABLE_VIA_EXCEPTION,
                    via_rule_id=firing_layer_exception,
                    founding_finding_ids=list(group["founding"]),
                )
            )
            continue

        # No layer exception → walk each founding rule.
        rule_outcomes: list[ComplianceStatus] = []
        for fid in group["founding"]:
            f = finding_by_id.get(fid)
            if f is None:
                continue
            f.rule_type = "founding"

            # rule_exception?
            firing_rule_ex: Optional[str] = None
            for ex_id in group["rule_exception_by_parent"].get(fid, []):
                ex_f = finding_by_id.get(ex_id)
                if ex_f and ex_f.status == ComplianceStatus.PASS:
                    firing_rule_ex = ex_id
                    break
            if firing_rule_ex:
                f.status = ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION
                f.via_rule_id = firing_rule_ex
                ex_finding = finding_by_id.get(firing_rule_ex)
                if ex_finding:
                    ex_finding.rule_type = "exception"
                rule_outcomes.append(f.status)
                continue

            if f.status == ComplianceStatus.PASS:
                # Founding passed outright. Nothing to do.
                rule_outcomes.append(f.status)
                continue

            # Founding FAIL/WARNING — try substitutions.
            sub_ids = group["substitution_by_parent"].get(fid, [])
            f.tried_substitution_ids = list(sub_ids)
            rescuer: Optional[str] = None
            for sub_id in sub_ids:
                sf = finding_by_id.get(sub_id)
                if sf and sf.status == ComplianceStatus.PASS:
                    rescuer = sub_id
                    break
            if rescuer:
                f.status = ComplianceStatus.PASS_VIA_SUBSTITUTION
                f.via_rule_id = rescuer
                rf = finding_by_id.get(rescuer)
                if rf:
                    rf.rule_type = "substitution"
            # else: FAIL stands. tried_substitution_ids already recorded.
            rule_outcomes.append(f.status)

        # Layer rollup.
        if not rule_outcomes:
            # No founding rules in this layer — skip emitting a rollup.
            continue
        layer_compliant_states = {
            ComplianceStatus.PASS,
            ComplianceStatus.PASS_VIA_SUBSTITUTION,
            ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION,
            ComplianceStatus.NOT_APPLICABLE,  # plain N/A, no exception rule
        }
        if all(s in layer_compliant_states for s in rule_outcomes):
            if any(s == ComplianceStatus.PASS_VIA_SUBSTITUTION for s in rule_outcomes):
                v = LayerVerdict.COMPLIANT_VIA_SUBSTITUTION
            elif all(s == ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION for s in rule_outcomes):
                v = LayerVerdict.NOT_APPLICABLE_VIA_EXCEPTION
            else:
                v = LayerVerdict.COMPLIANT
        else:
            v = LayerVerdict.NOT_COMPLIANT
        layer_verdicts.append(
            LayerVerdictRollup(
                tier=tier,
                layer=layer,
                verdict=v,
                via_rule_id=None,
                founding_finding_ids=list(group["founding"]),
            )
        )

    return findings, layer_verdicts


def _architecture_verdict_from_layers(layer_verdicts: list):
    """Roll layer verdicts up to a single architecture verdict.

    NOT_COMPLIANT if any layer is; else COMPLIANT_VIA_SUBSTITUTION if any
    layer was rescued by a substitution; else NOT_APPLICABLE_VIA_EXCEPTION
    if every layer was exempted; else COMPLIANT. None when there are no
    layer verdicts (nothing to roll up).
    """
    from app.models.validation import LayerVerdict

    if not layer_verdicts:
        return None
    if any(lv.verdict == LayerVerdict.NOT_COMPLIANT for lv in layer_verdicts):
        return LayerVerdict.NOT_COMPLIANT
    if any(
        lv.verdict == LayerVerdict.COMPLIANT_VIA_SUBSTITUTION
        for lv in layer_verdicts
    ):
        return LayerVerdict.COMPLIANT_VIA_SUBSTITUTION
    if all(
        lv.verdict == LayerVerdict.NOT_APPLICABLE_VIA_EXCEPTION
        for lv in layer_verdicts
    ):
        return LayerVerdict.NOT_APPLICABLE_VIA_EXCEPTION
    return LayerVerdict.COMPLIANT


def resolve_decision_tree(
    findings: list[Finding],
    standards: list[dict],
    tier_used: Optional[int],
):
    """Shared post-processing for ALL validation paths.

    Applies the founding/substitution/exception decision tree, rolls up
    per-layer verdicts, and derives the architecture verdict — the SAME
    semantics the chunked text path always had. The multimodal (diagram)
    and PPTX paths historically SKIPPED this (they returned before the
    call site), so most real SDDs (any with a diagram) got zero
    exception/substitution handling and no layer/architecture verdicts.
    Routing every path through this helper fixes that.

    Returns (findings, layer_verdicts, architecture_verdict).
    """
    findings, layer_verdicts = _apply_decision_tree(
        findings, standards, tier_used=tier_used
    )
    architecture_verdict = _architecture_verdict_from_layers(layer_verdicts)
    return findings, layer_verdicts, architecture_verdict


def _calculate_score(findings: list[Finding]) -> Optional[float]:
    """Weighted compliance score over findings, 0-100.

    Returns None when there is nothing to score (no findings, or every
    finding is excluded from the denominator). None means "not
    applicable / insufficient evidence" — NOT 100. Reporting 100 for a
    document that matched no evaluable rules is dangerously misleading
    (reads as a perfect pass), which was the pre-fix behavior.

    Denominator (rules that count for/against compliance): PASS,
    PASS_VIA_SUBSTITUTION, WARNING, FAIL.
    Excluded (neither earn nor add weight):
      - NOT_ADDRESSED — the document is silent; a coverage gap, not a
        violation. Surfaced separately so it isn't hidden, but it must
        not drag the score to 0 (the pre-fix FAIL-collapse bug).
      - NOT_APPLICABLE / NOT_APPLICABLE_VIA_EXCEPTION — the rule was
        exempted; counting it would penalise a legitimately-waived rule.
    Credit: PASS + PASS_VIA_SUBSTITUTION full weight, WARNING half.
    """
    if not findings:
        return None

    weights = {
        Severity.CRITICAL: 4.0,
        Severity.HIGH: 3.0,
        Severity.MEDIUM: 2.0,
        Severity.LOW: 1.0,
        Severity.INFO: 0.5,
    }
    # Statuses that don't count toward the score denominator.
    _EXCLUDED = {
        ComplianceStatus.NOT_ADDRESSED,
        ComplianceStatus.NOT_APPLICABLE,
        ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION,
    }
    _FULL_CREDIT = {
        ComplianceStatus.PASS,
        ComplianceStatus.PASS_VIA_SUBSTITUTION,
    }

    total = 0.0
    earned = 0.0
    for f in findings:
        if f.status in _EXCLUDED:
            continue
        w = weights.get(f.severity, 1.0)
        total += w
        if f.status in _FULL_CREDIT:
            earned += w
        elif f.status == ComplianceStatus.WARNING:
            earned += w * 0.5

    if total == 0:
        # Everything was excluded (all N/A or not-addressed) — no
        # evaluable rules, so there is no meaningful score.
        return None
    return round((earned / total) * 100, 1)


# ---------------------------------------------------------------------------
# Per-slide validator for PPTX-derived documents.
#
# Validates each (slide × rule_batch) where rule_batch is up to
# RULES_PER_SLIDE_BATCH rules at a time. A 30-slide deck × 100 rules
# becomes 30 × 10 = 300 multimodal calls, not 30 × 100 = 3000.
# Findings populate `evidence` (text-grounded) per slide.
# ---------------------------------------------------------------------------


RULES_PER_SLIDE_BATCH = int(os.getenv("RULES_PER_SLIDE_BATCH", "10"))


async def validate_pptx_document(
    document,
    standards: list[dict],
    *,
    document_content_hash: Optional[str] = None,
    standards_set_hash: Optional[str] = None,
    standards_set: str = "default",
    validation_id: Optional[str] = None,
    on_progress: Optional[Any] = None,
) -> ValidationResult:
    """Slide-mode parallel validator for PPTX-derived documents.

    Each (slide × rule_batch) is one multimodal Bedrock Converse
    call. The model returns a list of per-rule verdicts; we merge
    across slides per rule with the same FAIL > WARNING > PASS
    precedence as the chunk-mode validator.
    """
    import base64
    from app.models.document import SlideType
    from app.services.document_store import document_store

    started_at = datetime.utcnow()
    validation_id = validation_id or f"val-{uuid.uuid4().hex[:12]}"
    total_provided = len(standards)

    if not standards:
        raise ValueError("No standards provided for validation")
    if not document.slides:
        raise ValueError(
            f"PPTX validation requires document.slides; got None for {document.document_id}"
        )

    # Reuse the same tier + scope filtering as the chunk-mode path.
    declared_tier = getattr(document, "declared_tier", None)
    declared_scope = document.hosting_scope
    skipped_tier = 0
    skipped_scope = 0

    if declared_tier is not None:
        keep: list[dict] = []
        for s in standards:
            tiers = s.get("applies_to_tiers")
            if not tiers:
                keep.append(s)
                continue
            try:
                if int(declared_tier) in [int(t) for t in tiers]:
                    keep.append(s)
                else:
                    skipped_tier += 1
            except (TypeError, ValueError):
                keep.append(s)
        standards = keep
    standards = [
        s for s in standards
        if s["hosting_scope"] == declared_scope or s["hosting_scope"] == "both"
    ]
    skipped_scope = total_provided - skipped_tier - len(standards)

    skipped = skipped_tier + skipped_scope
    total = len(standards)
    if total == 0:
        # No rules apply after tier/scope filtering. Score is None
        # (not-applicable), NOT 100 — a doc that matched zero rules is
        # not "100% compliant". See _calculate_score.
        logger.info(
            f"PPTX validation {validation_id}: 0 applicable standards "
            f"(tier={declared_tier}, scope={declared_scope}, "
            f"provided={total_provided}, skipped_tier={skipped_tier}, "
            f"skipped_scope={skipped_scope})"
        )
        return ValidationResult(
            validation_id=validation_id,
            document_id=document.document_id,
            status=ValidationStatus.COMPLETED,
            standards_set=standards_set,
            started_at=started_at,
            completed_at=datetime.utcnow(),
            findings=[],
            overall_score=None,
            summary=(
                f"Not applicable — no standards apply to tier "
                f"{declared_tier} / scope {declared_scope} "
                f"(of {total_provided} total)."
            ),
            document_content_hash=document_content_hash,
            standards_set_hash=standards_set_hash,
            evaluated_count=0,
            total_standards=0,
            chunks_per_document=0,
            bedrock_calls=0,
            bedrock_input_tokens=0,
            bedrock_output_tokens=0,
            estimated_cost_usd=0.0,
            skipped_count=skipped,
            skipped_count_breakdown={"tier": skipped_tier, "scope": skipped_scope},
        )

    bedrock = _make_bedrock_client()
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    slides = document.slides

    # References block. Fetch active References once
    # and scope-filter per rule (same shape as the chunked-text and
    # multimodal paths). Failure here is non-fatal.
    try:
        from app.services.references_store import references_store
        all_refs_pptx = references_store.list_all(status="active")
    except Exception as exc:
        logger.warning(f"references_store unavailable; running without refs: {exc}")
        all_refs_pptx = []
    references_by_rule_pptx: dict[str, list[dict]] = {}
    if all_refs_pptx:
        from app.services.pdf_multimodal import _refs_for_rule
        for s in standards:
            rid = s.get("id")
            if not rid:
                continue
            matched = _refs_for_rule(s, all_refs_pptx)
            if matched:
                references_by_rule_pptx[rid] = matched

    # Batch standards into groups of RULES_PER_SLIDE_BATCH so each
    # multimodal call evaluates up to N rules at once. The expected
    # call count is len(slides) × ceil(len(standards) / batch).
    batches: list[list[dict]] = []
    for i in range(0, len(standards), RULES_PER_SLIDE_BATCH):
        batches.append(standards[i : i + RULES_PER_SLIDE_BATCH])

    expected_calls = len(slides) * len(batches)
    logger.info(
        f"Starting PPTX validation {validation_id}: "
        f"{total} rules × {len(slides)} slides via {len(batches)} batches "
        f"= {expected_calls} multimodal calls; concurrency={MAX_CONCURRENT}"
    )

    # Per-rule verdict accumulator: rule_id → list of (verdict, evidence,
    # image_ref) tuples across slides. We merge at the end.
    rule_partials: dict[str, list[dict]] = {s["id"]: [] for s in standards}
    bedrock_calls = 0
    in_tokens = 0
    out_tokens = 0
    preceding_title_text: Optional[str] = None

    # Build a quick lookup of slide_index → SlideStatus + image bytes.
    slide_records: list[dict] = []
    for ss in slides:
        slide_records.append(
            {
                "slide_status": ss,
                "image_bytes": (
                    document_store.download_document(ss.image_s3_key)
                    if ss.image_s3_key
                    else None
                ),
            }
        )

    async def evaluate_slide_batch(slide_record: dict, batch: list[dict]) -> None:
        nonlocal bedrock_calls, in_tokens, out_tokens
        ss = slide_record["slide_status"]
        if ss.slide_type == SlideType.TITLE_ONLY:
            # Title slides can't fail any rule; skip the call. The
            # text propagates to the next slide as preamble — handled
            # in the outer loop, not here.
            return
        prompt = _build_pptx_validation_prompt(
            slide_index=ss.slide_index,
            slide_count=len(slides),
            slide_type=ss.slide_type,
            preamble_text=preceding_title_text,
            rules=batch,
            references_by_rule=references_by_rule_pptx,
        )
        content_blocks: list = [{"text": prompt}]
        if slide_record["image_bytes"]:
            content_blocks.append(
                {
                    "image": {
                        "format": "png",
                        "source": {
                            "bytes": base64.standard_b64encode(
                                slide_record["image_bytes"]
                            ).decode("ascii"),
                        },
                    }
                }
            )
        # Extended thinking on PPTX path. Default ON since
        # PPTX call count is bounded by slide count (modest cost).
        # When on, Bedrock requires temperature=1.0 and
        # maxTokens > budget_tokens.
        from app.services.pdf_multimodal import (
            _thinking_enabled_pptx,
            _thinking_request_fields,
            _extract_reasoning_blocks,
            THINKING_BUDGET_TOKENS,
        )
        thinking_on = _thinking_enabled_pptx()
        inference_temp = 1.0 if thinking_on else 0.1
        max_tokens_pptx = (THINKING_BUDGET_TOKENS + 4000) if thinking_on else 4000
        extra_request_kwargs = _thinking_request_fields() if thinking_on else {}
        pptx_model_id = model_for("PPTX_VALIDATION_MODEL_ID", SONNET)

        import time as _time_pptx
        async with sem:
            try:
                t0_pptx = _time_pptx.monotonic()
                resp = await asyncio.to_thread(
                    bedrock.converse,
                    modelId=pptx_model_id,
                    messages=[{"role": "user", "content": content_blocks}],
                    inferenceConfig={
                        "maxTokens": max_tokens_pptx,
                        "temperature": inference_temp,
                    },
                    **extra_request_kwargs,
                )
                latency_ms_pptx = int((_time_pptx.monotonic() - t0_pptx) * 1000)
            except Exception as exc:
                logger.warning(
                    f"slide {ss.slide_index} batch failed: {exc}"
                )
                return
        bedrock_calls += 1
        usage = resp.get("usage", {})
        call_in_tokens = int(usage.get("inputTokens", 0))
        call_out_tokens = int(usage.get("outputTokens", 0))
        in_tokens += call_in_tokens
        out_tokens += call_out_tokens
        content = resp.get("output", {}).get("message", {}).get("content", [])
        text = "".join(blk.get("text", "") for blk in content)
        # Capture reasoning from the slide-batch call. The
        # PPTX merge will surface it on the winning slide's Finding.
        reasoning_text, reasoning_signature = _extract_reasoning_blocks(content)
        # Build a chunk_excerpt for the trace. The model saw the
        # rendered slide image (when present) plus the prompt body
        # which already embeds the slide's native text + preamble.
        # Showing the prompt's slide-section as the excerpt is the
        # cleanest "what the model saw" we can render.
        slide_text_excerpt = (
            f"slide {ss.slide_index} ({ss.slide_type.value if hasattr(ss.slide_type, 'value') else ss.slide_type})"
            + ("\n[image attached]" if slide_record.get("image_bytes") else "")
        )
        parsed = _extract_json(text) or {}
        for entry in parsed.get("verdicts", []) or []:
            rid = entry.get("requirement_id")
            if rid not in rule_partials:
                continue
            evidence_text = entry.get("evidence", "") or ""
            image_desc = entry.get("image_description")
            rule_partials[rid].append(
                {
                    "slide_index": ss.slide_index,
                    "verdict": (entry.get("status") or "PASS").upper(),
                    "evidence": evidence_text,
                    "image_description": image_desc,
                    "image_s3_key": ss.image_s3_key,
                    "is_diagram_evidence": ss.slide_type
                    in (SlideType.DIAGRAM_HEAVY, SlideType.HYBRID)
                    and bool(image_desc),
                    # Trace data per slide-batch call.
                    "prompt": prompt,
                    "raw_response": text[:2000],
                    "chunk_excerpt": slide_text_excerpt,
                    "reasoning_text": reasoning_text,
                    "reasoning_signature": reasoning_signature,
                    "input_tokens": call_in_tokens,
                    "output_tokens": call_out_tokens,
                    "latency_ms": latency_ms_pptx,
                    "model_id": pptx_model_id,
                }
            )

    # Sequential outer loop over slides so preceding_title_text is
    # always the closest preceding title, and rule batches per slide
    # run in parallel under the semaphore.
    for slide_record in slide_records:
        # Cooperative cancellation checkpoint between slides.
        if is_cancel_requested(validation_id):
            logger.info(f"PPTX validation {validation_id} cancelled by user")
            raise ValidationCancelled(validation_id)
        ss = slide_record["slide_status"]
        if ss.slide_type == SlideType.TITLE_ONLY:
            preceding_title_text = next(
                (
                    s.native_text
                    for s in document.slides
                    if s.slide_index == ss.slide_index
                ),
                None,
            )
            continue
        await asyncio.gather(
            *[evaluate_slide_batch(slide_record, b) for b in batches]
        )
        preceding_title_text = None
        if on_progress is not None:
            try:
                maybe_coro = on_progress(
                    sum(1 for v in rule_partials.values() if v),
                    total,
                )
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception:
                logger.exception("Progress callback raised; ignoring")

    # Post-loop cancellation check — a single-slide deck's in-flight call
    # can't be interrupted mid-flight; discard rather than persist a
    # completed run the user cancelled.
    if is_cancel_requested(validation_id):
        logger.info(f"PPTX validation {validation_id} cancelled by user (post-loop)")
        raise ValidationCancelled(validation_id)

    # Merge per-rule partials into Findings with FAIL > WARNING > PASS
    # precedence. Image evidence is structured; text evidence stays
    # in the existing `evidence` field.
    findings: list[Finding] = []
    severity_rank = {"FAIL": 0, "WARNING": 1, "PASS": 2}
    for std in standards:
        rid = std["id"]
        partials = rule_partials.get(rid, [])
        if not partials:
            # No slide addressed this rule — NOT_ADDRESSED (a coverage
            # gap), not a WARNING/FAIL. Consistent with the chunked +
            # multimodal paths; excluded from the score denominator.
            findings.append(
                Finding(
                    finding_id=f"find-{uuid.uuid4().hex[:8]}",
                    requirement_id=rid,
                    requirement_title=std.get("name", rid),
                    severity=std.get("severity", "medium"),
                    status=ComplianceStatus.NOT_ADDRESSED,
                    evidence="No evidence found across the deck.",
                    recommendation=std.get("recommendation", "") or "",
                )
            )
            continue
        partials.sort(
            key=lambda p: (severity_rank.get(p["verdict"], 99), p["slide_index"])
        )
        winner = partials[0]
        text_parts = [
            f"slide {p['slide_index']}: {p['evidence']}"
            for p in partials
            if p["evidence"]
        ]
        # The PPTX path used to emit Finding.trace = None for
        # every finding. Now we build
        # the trace from the winning partial — same shape as the
        # multimodal/chunked paths so the trace drawer + chat panel
        # render identically across validators.
        trace = {
            "prompt": winner.get("prompt", "") or "",
            "chunk_excerpt": winner.get("chunk_excerpt", "") or "",
            "raw_response": winner.get("raw_response", "") or "",
            "input_tokens": int(winner.get("input_tokens") or 0),
            "output_tokens": int(winner.get("output_tokens") or 0),
            "latency_ms": int(winner.get("latency_ms") or 0),
            "reasoning": winner.get("reasoning_text") or None,
            "reasoning_signature": winner.get("reasoning_signature"),
            "model_id": winner.get("model_id"),
        } if winner.get("prompt") else None

        finding = Finding(
            finding_id=f"find-{uuid.uuid4().hex[:8]}",
            requirement_id=rid,
            requirement_title=std.get("name", rid),
            severity=std.get("severity", "medium"),
            status=winner["verdict"].lower(),
            evidence="\n".join(text_parts) or "(diagram-only evidence)",
            recommendation=std.get("recommendation", "") or "",
            also_seen_pages=[
                f"slide {p['slide_index']}"
                for p in partials[1:]
                if p["verdict"] == winner["verdict"]
            ],
            trace=trace,
        )
        findings.append(finding)

    # Adversarial verify pass for FAIL/critical findings — consistent with
    # the chunked + multimodal paths. Runs before the decision tree so a
    # downgraded FAIL->WARNING flows into the rollup; verify tokens fold into
    # cost telemetry.
    pptx_std_by_id = {s.get("id"): s for s in standards if s.get("id")}
    v_calls, v_in, v_out = await _verify_findings_async(
        bedrock, findings, pptx_std_by_id, sem
    )
    bedrock_calls += v_calls
    in_tokens += v_in
    out_tokens += v_out

    # The PPTX path historically
    # SKIPPED the decision tree (like the multimodal path), so decks got
    # zero founding/substitution/exception handling and no layer /
    # architecture verdicts. Run the SAME shared resolution the chunked
    # path uses. tier = declared_tier (PPTX has no tier_override param).
    findings, layer_verdicts, architecture_verdict = resolve_decision_tree(
        findings, standards, tier_used=declared_tier
    )

    overall_score = _calculate_score(findings)
    completed_at = datetime.utcnow()
    elapsed = (completed_at - started_at).total_seconds()
    estimated_cost = round(
        (in_tokens / 1_000_000) * BEDROCK_INPUT_USD_PER_MTOK
        + (out_tokens / 1_000_000) * BEDROCK_OUTPUT_USD_PER_MTOK,
        4,
    )
    logger.info(
        f"Completed PPTX validation {validation_id} in {elapsed:.1f}s: "
        f"score={overall_score}, findings={len(findings)}, "
        f"slides={len(slides)}, batches={len(batches)}, "
        f"calls={bedrock_calls}, in_tokens={in_tokens}, "
        f"out_tokens={out_tokens}, cost=${estimated_cost:.4f}"
    )

    return ValidationResult(
        validation_id=validation_id,
        document_id=document.document_id,
        status=ValidationStatus.COMPLETED,
        standards_set=standards_set,
        started_at=started_at,
        completed_at=completed_at,
        findings=findings,
        overall_score=overall_score,
        layer_verdicts=layer_verdicts,
        architecture_verdict=architecture_verdict,
        tier_used=declared_tier,
        summary=(
            f"Validated {len(findings)} rules across {len(slides)} slides "
            f"({bedrock_calls} multimodal calls, "
            f"${estimated_cost:.4f} est. cost)."
        ),
        document_content_hash=document_content_hash,
        standards_set_hash=standards_set_hash,
        evaluated_count=len(findings),
        total_standards=len(findings),
        chunks_per_document=len(slides),
        bedrock_calls=bedrock_calls,
        bedrock_input_tokens=in_tokens,
        bedrock_output_tokens=out_tokens,
        estimated_cost_usd=estimated_cost,
        skipped_count=skipped,
        skipped_count_breakdown={"tier": skipped_tier, "scope": skipped_scope},
    )


def _build_pptx_validation_prompt(
    slide_index: int,
    slide_count: int,
    slide_type,
    preamble_text: Optional[str],
    rules: list[dict],
    references_by_rule: Optional[dict[str, list[dict]]] = None,
) -> str:
    """Compose the user-message text for a (slide × rule_batch) call."""
    # Serialize each rule as a JSON object via json.dumps so embedded quotes
    # in criteria/source text don't break the payload (the old f-string
    # interpolation could). Enrich with description + verbatim source_quote +
    # rule_type note — fields the PPTX path previously dropped — for grounding.
    def _rule_obj(r: dict) -> dict:
        obj = {
            "id": r.get("id"),
            "name": r.get("name", ""),
            "validation_criteria": r.get("validation_criteria", ""),
        }
        if (r.get("description") or "").strip():
            obj["description"] = r["description"].strip()
        if (r.get("alternative_criteria") or "").strip():
            obj["alternatives_any_also_pass"] = r["alternative_criteria"].strip()
        if (r.get("source_quote") or "").strip():
            obj["source_text_verbatim"] = r["source_quote"].strip()[:600]
        rt = (r.get("rule_type") or "").lower()
        if rt == "exception":
            obj["rule_type_note"] = "EXCEPTION: 'pass' means the doc meets the exemption (parent becomes N/A)"
        elif rt == "substitution":
            obj["rule_type_note"] = "SUBSTITUTION: 'pass' means the doc uses this alternative to the parent"
        return obj

    rule_lines = "\n".join(f"  {json.dumps(_rule_obj(r))}" for r in rules)
    pre = f"\nPRECEDING TITLE: {preamble_text.strip()}\n" if preamble_text else ""
    # Reuse the multimodal helper so the chunked-text,
    # multimodal whole-doc, and PPTX per-slide paths render References
    # identically (same heading, same per-rule attribution, same
    # "factual context, NOT rules" guardrail).
    from app.services.pdf_multimodal import _format_references_block
    references_block = _format_references_block(rules, references_by_rule)
    return (
        f"You are validating an architecture deck. This is slide "
        f"{slide_index} of {slide_count} (type={getattr(slide_type, 'value', slide_type)}).\n"
        f"{pre}\n"
        f"{references_block}"
        "For EACH rule below, decide whether the slide demonstrates "
        "compliance (PASS), a clear violation (FAIL), or simply does "
        "not address this rule (WARNING means insufficient evidence).\n\n"
        "Return JSON in this exact shape:\n"
        '{\n  "verdicts": [\n'
        '    {"requirement_id": "...", "status": "PASS|FAIL|WARNING",\n'
        '     "evidence": "verbatim text excerpt or empty string",\n'
        '     "image_description": "what the diagram shows that supports this verdict, or null"}\n'
        "  ]\n}\n\n"
        f"RULES TO EVALUATE:\n[\n{rule_lines}\n]\n\n"
        "Return ONLY the JSON, no commentary."
    )
