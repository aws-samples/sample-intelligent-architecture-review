# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Multimodal PDF validator.

Renders each PDF page to a PNG and sends (page text + page image)
to Bedrock Converse per (page × rule_batch). Mirrors the structure
of `validate_pptx_document` in `parallel_validator.py` but works
per-page instead of per-slide.

Designed to live alongside `parallel_validator.py` — the dispatcher
in that module routes diagram-bearing PDFs here. Pure-text PDFs (no
embedded images) keep the cheaper text-only path.
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from app.bedrock_models import SONNET, model_for
from app.services.prompt_safety import UNTRUSTED_DATA_NOTICE, untrusted_block

logger = logging.getLogger(__name__)


def _refs_for_rule(rule: dict, all_refs: list[dict]) -> list[dict]:
    """Scope-filter References against one rule.

    Mirrors parallel_validator._refs_for() byte-for-byte so the chunked
    text path and the multimodal path agree on which References attach
    to which rule. The match logic:

      - tier:  rule's applies_to_tiers ∩ ref's applies_to_tiers must be
        non-empty (or either side may be unset, which means "all").
      - layer: when both rule and ref name a layer, they must match.
      - hosting_scope: ref scope must be "both" or equal to rule scope.

    Returns the subset of all_refs that match this rule.
    """
    if not all_refs:
        return []
    rule_tiers = {int(t) for t in (rule.get("applies_to_tiers") or [])}
    rule_layer = rule.get("layer")
    rule_scope = rule.get("hosting_scope") or "both"
    out: list[dict] = []
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


def _format_references_block(
    rule_batch: list[dict],
    references_by_rule: Optional[dict[str, list[dict]]] = None,
) -> str:
    """Render the union of scope-matching References across a rule batch.

    Returns a markdown block (terminated with \\n\\n) when at least one
    rule in the batch has matching References, or an empty string. The
    block heads with the same guardrail wording the chunked-text path
    uses ("factual context, NOT rules to validate against") so the
    model doesn't try to grade the references themselves.

    A single Reference may apply to multiple rules in the batch; we
    deduplicate on ref id so its body appears once, with a "for: R-01,
    R-04" header so the model knows which rules it's contextualising.
    """
    if not references_by_rule:
        return ""
    # ref_id -> (ref, set(rule_ids))
    grouped: dict[str, tuple[dict, set[str]]] = {}
    for r in rule_batch:
        rid = r.get("id")
        if not rid:
            continue
        for ref in references_by_rule.get(rid, []) or []:
            ref_id = ref.get("id")
            if not ref_id:
                continue
            if ref_id not in grouped:
                grouped[ref_id] = (ref, set())
            grouped[ref_id][1].add(rid)
    if not grouped:
        return ""
    lines = [
        "--- References (factual context, NOT rules to validate against) ---",
        UNTRUSTED_DATA_NOTICE,
    ]
    for ref, applied_rules in grouped.values():
        # Fenced, not interpolated as headings — see prompt_safety for why.
        lines.append("\n" + untrusted_block(
            ref.get("body"), "reference",
            name=ref.get("name") or "(reference)",
            applies_to=", ".join(sorted(applied_rules)),
        ))
    return "\n".join(lines) + "\n\n"


def _build_page_message(
    *,
    page_number: int,
    page_text: str,
    page_png: Optional[bytes],
    rule_batch: list[dict],
) -> dict:
    """Build a single Bedrock Converse `messages` entry that carries
    the page text + the rendered page PNG + the rule batch.

    Bedrock expects:
        {
          "role": "user",
          "content": [
             {"text": "..."},
             {"image": {"format": "png", "source": {"bytes": <raw>}}},
          ]
        }

    Image bytes are passed RAW; boto3 base64-encodes them on the wire.
    Double-encoding triggers "Could not process image" from the model.
    """
    rule_lines = []
    for r in rule_batch:
        rule_lines.append(
            f"- {r.get('id')}: {r.get('name', '')}"
            f"\n  Severity: {r.get('severity', '')}"
            f"\n  Validation criteria: {r.get('validation_criteria', '')}"
        )
    rules_block = "\n".join(rule_lines) if rule_lines else "(no rules in batch)"

    text_block = (
        f"You are validating a System Design Document, page {page_number}, "
        f"against High Availability rules. The page may contain an "
        f"architecture diagram — examine it visually and cite topology "
        f"facts (component shapes, arrows, redundancy patterns) when "
        f"relevant.\n\n"
        f"--- Page {page_number} text ---\n{page_text}\n\n"
        f"--- Rules to evaluate on this page ---\n{rules_block}\n\n"
        f"For EACH rule, respond with JSON: {{\"rule_id\": ..., "
        f"\"verdict\": \"pass|fail|warning|not_addressed\", "
        f"\"evidence\": \"...\", \"recommendation\": \"...\", "
        f"\"affected_components\": [...]}}. "
        f"Return a JSON array of these objects, one per rule, even when "
        f"the verdict is not_addressed."
    )

    content: list[dict] = [{"text": text_block}]
    if page_png:
        content.append(
            {
                "image": {
                    "format": "png",
                    "source": {"bytes": page_png},
                }
            }
        )
    return {"role": "user", "content": content}


def _build_whole_doc_message(
    *,
    page_texts: list[str],
    page_pngs: list[bytes],
    diagram_page_indices: list[int],
    rule_batch: list[dict],
    references_by_rule: Optional[dict[str, list[dict]]] = None,
    hosting_scope: Optional[str] = None,
) -> dict:
    """Whole-doc Bedrock Converse message.

    The model sees the full text of every page concatenated with
    `[Page N]` markers, plus up to MAX_IMAGES_PER_CALL rendered page
    images (we prefer pages that contain diagrams). The rule batch
    is appended as instructions for what to evaluate.

    This is the "how Claude reads it when you share a PDF" shape:
    one prompt, all content, model reasons across pages.

    Pages beyond the image cap go in text-only — the model still
    reads them, just doesn't see their visuals.

    references_by_rule, when provided, attaches per-rule scope-matching
    References as factual context (NOT rules). The block guards the
    model against grading the references themselves.
    """
    rule_lines = []
    for r in rule_batch:
        line = (
            f"- {r.get('id')}: {r.get('name', '')}"
            f"\n  Severity: {r.get('severity', '')}"
            f"\n  Validation criteria: {r.get('validation_criteria', '')}"
        )
        # Enrich with the fields the multimodal path previously dropped —
        # description, alternatives, and the verbatim source text — so the
        # model grounds its verdict on the rule's full intent + actual wording.
        desc = (r.get("description") or "").strip()
        if desc:
            line += f"\n  Description: {desc}"
        alt = (r.get("alternative_criteria") or "").strip()
        if alt:
            line += f"\n  Alternatives (any also pass): {alt}"
        sq = (r.get("source_quote") or "").strip()
        if sq:
            line += f"\n  Source text (verbatim): \"{sq[:600]}\""
        rt = (r.get("rule_type") or "").lower()
        if rt == "exception":
            line += "\n  NOTE: EXCEPTION rule — 'pass' means the doc meets the exemption (parent becomes N/A)."
        elif rt == "substitution":
            line += "\n  NOTE: SUBSTITUTION rule — 'pass' means the doc uses this alternative to the parent."
        rule_lines.append(line)
    rules_block = "\n".join(rule_lines) if rule_lines else "(no rules in batch)"
    references_block = _format_references_block(rule_batch, references_by_rule)

    # Concatenate all page text with markers so the model knows where
    # one page ends and the next begins, and can cite "Page N" in
    # evidence.
    full_doc_text_parts = []
    for i, t in enumerate(page_texts, start=1):
        if t and t.strip():
            full_doc_text_parts.append(f"[Page {i}]\n{t.strip()}")
    full_doc_text = "\n\n".join(full_doc_text_parts)

    # Pick which pages get image embeds. Prefer diagram-bearing pages
    # (caller's hint) but fill remaining slots with sequential pages
    # so we still get visual context on the doc as a whole.
    selected_image_idx: list[int] = []
    seen: set[int] = set()
    for idx in diagram_page_indices:
        if idx < len(page_pngs) and idx not in seen:
            selected_image_idx.append(idx)
            seen.add(idx)
            if len(selected_image_idx) == MAX_IMAGES_PER_CALL:
                break
    if len(selected_image_idx) < MAX_IMAGES_PER_CALL:
        # Top-up with any remaining pages in order, until cap.
        for idx in range(len(page_pngs)):
            if idx in seen:
                continue
            selected_image_idx.append(idx)
            seen.add(idx)
            if len(selected_image_idx) == MAX_IMAGES_PER_CALL:
                break
    selected_image_idx.sort()

    image_pages_note = (
        f"Page images attached (in order): "
        f"{[i + 1 for i in selected_image_idx] or 'none'}. "
        f"For pages not attached as images, you have only the text."
        if selected_image_idx
        else "No page images attached; reasoning from text only."
    )

    text_block = (
        f"You are validating a System Design Document against High "
        f"Availability rules. The document spans {len(page_texts)} "
        f"pages; below is the full text of every page, followed by "
        f"rendered images of selected pages so you can examine "
        f"architecture diagrams.\n\n"
        f"--- Full document text ---\n{full_doc_text}\n\n"
        f"--- {image_pages_note} ---\n\n"
        f"{references_block}"
        f"--- Rules to evaluate against the WHOLE document ---\n"
        f"{rules_block}\n\n"
        f"For EACH rule, respond with JSON: {{\"rule_id\": ..., "
        f"\"verdict\": \"pass|fail|warning|not_addressed\", "
        f"\"evidence\": \"cite specific page numbers and what you "
        f"saw, including diagram details when relevant\", "
        f"\"recommendation\": \"...\", "
        f"\"affected_components\": [...]}}. "
        f"Return a JSON array of these objects, one per rule."
    )

    # Custom icon legend — teach the validator the org's on-prem
    # glyph vocabulary so it reads proprietary icons the same way extraction
    # did. Text dictionary appended to the prompt; icon-image few-shot blocks
    # placed before the page images so they read as a reference legend.
    # Best-effort: no legend → no change.
    legend_text = ""
    legend_image_blocks: list = []
    try:
        from app.services.icon_legend_inject import (
            build_legend_text,
            build_legend_image_blocks,
        )
        legend_text = build_legend_text(hosting_scope=hosting_scope)
        if legend_text:
            from app.services.document_store import document_store
            legend_image_blocks = build_legend_image_blocks(
                hosting_scope=hosting_scope, doc_store=document_store
            )
    except Exception:  # noqa: BLE001
        legend_text = ""
        legend_image_blocks = []

    text_with_legend = f"{text_block}\n\n{legend_text}" if legend_text else text_block
    content: list[dict] = [{"text": text_with_legend}]
    content.extend(legend_image_blocks)
    for idx in selected_image_idx:
        content.append(
            {
                "image": {
                    "format": "png",
                    "source": {"bytes": page_pngs[idx]},
                }
            }
        )
    return {"role": "user", "content": content}


# ---------------------------------------------------------------------------
# Multimodal validator entry point
# ---------------------------------------------------------------------------

import json
import re
import time as _time
import uuid
from datetime import datetime

from app.models.validation import (
    ComplianceStatus,
    Finding,
    Severity,
    ValidationResult,
    ValidationStatus,
)
from app.utils.pdf_parser import render_pages_to_png

RULES_PER_PAGE_BATCH = 5

# Whole-document multimodal validation.
# Bedrock Converse caps at 20 images per call; everything beyond
# that goes in as text-only. Rules are batched so we don't blow
# the output token budget; one Converse call per rule batch.
MAX_IMAGES_PER_CALL = 20
RULES_PER_DOC_BATCH = int(os.getenv("RULES_PER_DOC_BATCH", "8"))

# Per-path thinking kill switches. A single global flag would force
# an all-or-nothing decision on operators; the chunked path's
# +$24-32/run cost cliff would push them to disable everything.
# Per-path defaults preserve the cheap-path benefit.
THINKING_BUDGET_TOKENS = int(os.getenv("THINKING_BUDGET_TOKENS", "8000"))


def _thinking_enabled_multimodal() -> bool:
    """Default ON — multimodal path's bump is small (+$0.66-0.88/run)."""
    return os.getenv("ENABLE_THINKING_MULTIMODAL", "true").lower() in ("true", "1", "yes")


def _thinking_enabled_pptx() -> bool:
    """Default ON — PPTX is bounded by slide count, modest cost."""
    return os.getenv("ENABLE_THINKING_PPTX", "true").lower() in ("true", "1", "yes")


def _thinking_enabled_chunked() -> bool:
    """Default OFF — chunked path's cost cliff (+$24-32/run worst-case
    on an 80-rule × 5-chunk doc) is opt-in. Operators flip this on
    when they need reasoning capture for text-only SDDs."""
    return os.getenv("ENABLE_THINKING_CHUNKED", "false").lower() in ("true", "1", "yes")


def _thinking_request_fields() -> dict:
    """The `additionalModelRequestFields` payload Bedrock expects when
    extended thinking is enabled. Returned as a dict that callers
    splat into the Converse call."""
    return {
        "additionalModelRequestFields": {
            "thinking": {
                "type": "enabled",
                "budget_tokens": THINKING_BUDGET_TOKENS,
            }
        }
    }


def _make_bedrock_client():
    """Indirection so tests can monkeypatch this symbol."""
    from app.services.parallel_validator import _make_bedrock_client as _f

    return _f()


def _extract_reasoning_blocks(blocks: list[dict]) -> tuple[str, Optional[str]]:
    """Pull the model's verbalized reasoning out of a Bedrock
    Converse response's content blocks.

    Verified (2026-05-27) that both
    `claude-sonnet-4-20250514-v1:0` and `claude-sonnet-4-6` use this shape:

        {"reasoningContent": {"reasoningText": {"text": "...", "signature": "..."}}}

    Returns (reasoning_text, signature). Empty string + None when the
    model didn't emit a reasoning block (thinking off, short response,
    or an older response shape). Concatenates across multiple reasoning
    blocks (defensive — the spec allows it though it's rare).

    The signature is opaque: we store it because tool-use thinking
    requires round-tripping it, but we never interpret it.
    """
    parts: list[str] = []
    signature: Optional[str] = None
    for b in blocks:
        rc = b.get("reasoningContent") if isinstance(b, dict) else None
        if not rc:
            continue
        rt = rc.get("reasoningText") if isinstance(rc, dict) else None
        if not rt:
            continue
        text = rt.get("text") or ""
        if text:
            parts.append(text)
        if signature is None:
            sig = rt.get("signature")
            if sig:
                signature = sig
    return "".join(parts), signature


def _extract_json_array(text: str) -> list[dict]:
    """Pull the first JSON array out of *text*. The model occasionally
    wraps the array in markdown fences, adds chatty preamble, or has
    its response truncated at maxTokens mid-array. Recover what we can.

    Strategy:
      1. Try a fenced ```json [ ... ] ``` block (closed, well-formed).
      2. Fall back to first [ ... last ].
      3. If neither yields a parseable array, walk the string and
         extract every {...} object that itself parses — this rescues
         truncated responses that emit N complete objects then cut off
         in the middle of object N+1.
    """
    # 1. Closed fenced block.
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # 2. Greedy [ ... ] across the whole text.
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # 3. Truncation rescue. Find the opening '[' (or fence open) and
    #    walk top-level {...} objects, parsing each one. The last
    #    incomplete object is dropped silently.
    if start == -1:
        return []
    depth = 0
    obj_start = -1
    objects: list[dict] = []
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start != -1:
                snippet = text[obj_start : i + 1]
                try:
                    parsed_obj = json.loads(snippet)
                    if isinstance(parsed_obj, dict):
                        objects.append(parsed_obj)
                except json.JSONDecodeError:
                    pass
                obj_start = -1
    return objects


def _severity_for(raw: str) -> Severity:
    return {
        "critical": Severity.CRITICAL,
        "high": Severity.HIGH,
        "medium": Severity.MEDIUM,
        "low": Severity.LOW,
        "info": Severity.INFO,
    }.get(str(raw).lower(), Severity.MEDIUM)


def _status_for(raw: str) -> ComplianceStatus:
    return {
        "pass": ComplianceStatus.PASS,
        "fail": ComplianceStatus.FAIL,
        "warning": ComplianceStatus.WARNING,
        # A model that reports "not_addressed" means the doc is silent on
        # the rule — a coverage gap, NOT a violation. Mapping it to FAIL
        # (the pre-fix behavior) collapsed multimodal scores to 0.
        "not_addressed": ComplianceStatus.NOT_ADDRESSED,
    }.get(str(raw).lower(), ComplianceStatus.WARNING)


def _rank(s: ComplianceStatus) -> int:
    # Merge precedence when a rule gets verdicts on multiple pages:
    # FAIL > WARNING > PASS > NOT_ADDRESSED. A real verdict on any page
    # outranks NOT_ADDRESSED elsewhere (the rule WAS addressed somewhere).
    if s == ComplianceStatus.FAIL:
        return 3
    if s == ComplianceStatus.WARNING:
        return 2
    if s == ComplianceStatus.PASS:
        return 1
    return 0  # NOT_ADDRESSED / anything else


async def _run_one_call(
    *,
    client,
    model_id: str,
    page_idx: int,
    page_text: str,
    page_png: bytes,
    batch: list[dict],
    sem,
) -> tuple[list[dict], dict, int, int, int, str, str]:
    """Run one (page × batch) Bedrock call concurrently. Returns
    (verdicts, prompt_text, raw_text, in_tokens, out_tokens, latency_ms, page_text)."""
    import asyncio as _asyncio

    msg = _build_page_message(
        page_number=page_idx,
        page_text=page_text,
        page_png=page_png,
        rule_batch=batch,
    )
    async with sem:
        t0 = _time.monotonic()
        try:
            resp = await _asyncio.to_thread(
                client.converse,
                modelId=model_id,
                messages=[msg],
                inferenceConfig={"maxTokens": 4000, "temperature": 0.0},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"multimodal page {page_idx} batch failed: {exc}"
            )
            return [], msg["content"][0]["text"], "", 0, 0, 0, page_text
    latency_ms = int((_time.monotonic() - t0) * 1000)
    usage = resp.get("usage") or {}
    in_tokens = int(usage.get("inputTokens", 0))
    out_tokens = int(usage.get("outputTokens", 0))
    blocks = resp.get("output", {}).get("message", {}).get("content", [])
    raw_text = "".join(b.get("text", "") for b in blocks if "text" in b)
    verdicts = _extract_json_array(raw_text)
    # Debug: surface parse failures so we don't end up with silent
    # zero-finding validations again.
    if not verdicts:
        logger.warning(
            f"multimodal page {page_idx}: model returned text but "
            f"_extract_json_array yielded 0 verdicts. raw[:300]={raw_text[:300]!r}"
        )
    return (
        verdicts,
        msg["content"][0]["text"],
        raw_text,
        in_tokens,
        out_tokens,
        latency_ms,
        page_text,
    )


async def _validate_pdf_multimodal_async(
    *,
    pdf_bytes: bytes,
    page_texts: list[str],
    document_id: str,
    standards: list[dict],
    declared_tier: Optional[int] = None,
    declared_scope: Optional[str] = None,
    standards_set: str = "default",
    on_progress=None,
    validation_id: Optional[str] = None,
) -> ValidationResult:
    """Async, concurrent version of the multimodal validator.

    validation_id, when supplied by the caller, is used both on the result
    and for cooperative cancellation checks. When omitted (e.g. direct test
    calls) a fresh id is minted.
    """
    import asyncio as _asyncio

    started_at = datetime.utcnow()
    if not validation_id:
        validation_id = f"val-{uuid.uuid4().hex[:12]}"

    # Tier + scope filter — mirrors parallel_validator dispatch.
    if declared_tier is not None:
        keep: list[dict] = []
        for s in standards:
            tiers = s.get("applies_to_tiers")
            if not tiers or int(declared_tier) in [int(t) for t in tiers]:
                keep.append(s)
        standards = keep
    if declared_scope is not None:
        standards = [
            s for s in standards
            if s.get("hosting_scope") in (declared_scope, "both", None)
        ]

    if not standards:
        # No applicable rules → score None (not-applicable), NOT 100.
        # See parallel_validator._calculate_score.
        logger.info(
            f"Multimodal validation {validation_id}: 0 applicable "
            f"standards (tier={declared_tier}, scope={declared_scope})"
        )
        return ValidationResult(
            validation_id=validation_id,
            document_id=document_id,
            status=ValidationStatus.COMPLETED,
            standards_set=standards_set,
            started_at=started_at,
            completed_at=datetime.utcnow(),
            findings=[],
            overall_score=None,
            summary="Not applicable — no standards apply to this document.",
            evaluated_count=0,
            total_standards=0,
        )

    # References block. Fetch active References once and
    # scope-filter per rule. Mirrors parallel_validator's chunked path.
    # Failure here is non-fatal: validation runs without refs.
    try:
        from app.services.references_store import references_store
        all_refs = references_store.list_all(status="active")
    except Exception as exc:
        logger.warning(f"references_store unavailable; running without refs: {exc}")
        all_refs = []
    references_by_rule: dict[str, list[dict]] = {}
    if all_refs:
        for s in standards:
            rid = s.get("id")
            if not rid:
                continue
            matched = _refs_for_rule(s, all_refs)
            if matched:
                references_by_rule[rid] = matched

    page_pngs = render_pages_to_png(pdf_bytes, dpi=150)
    if len(page_pngs) != len(page_texts):
        n = min(len(page_pngs), len(page_texts))
        page_pngs = page_pngs[:n]
        page_texts = page_texts[:n]

    # Detect diagram-bearing pages (used to prioritise which pages
    # get image embeds when the doc exceeds MAX_IMAGES_PER_CALL).
    from app.utils.pdf_parser import has_diagram_pages  # noqa: F401
    import fitz as _fitz
    diagram_indices: list[int] = []
    try:
        doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            for i, page in enumerate(doc):
                if page.get_images(full=False):
                    diagram_indices.append(i)
        finally:
            doc.close()
    except Exception:
        diagram_indices = []

    # Whole-doc batches: one Converse call per rule batch, each
    # carrying the full document (text + up to 20 page images).
    batches: list[list[dict]] = []
    for i in range(0, len(standards), RULES_PER_DOC_BATCH):
        batches.append(standards[i : i + RULES_PER_DOC_BATCH])

    client = _make_bedrock_client()
    # Env-overridable, defaulting to Sonnet 4.6 to match the chunked +
    # PPTX paths. The May-2025 Sonnet 4.0 model id previously hardcoded
    # here predated 4.6 and was an inconsistency: chunked + PPTX both
    # run Sonnet 4.6 via env defaults, only this path was stuck on the
    # older line. Both Sonnet 4.6 and 4.0 support extended thinking, so
    # the upgrade is safe.
    model_id = model_for("MULTIMODAL_VALIDATION_MODEL_ID", SONNET)

    # Its own knob, deliberately lower than the text validator's: each call here
    # carries page images, so the same concurrency costs far more tokens and hits
    # Bedrock's per-model limits sooner. Sharing one env name with the text path
    # meant the two could never be tuned apart.
    max_concurrent = int(os.getenv("MULTIMODAL_MAX_CONCURRENT", "10"))
    sem = _asyncio.Semaphore(max_concurrent)

    logger.info(
        f"whole-doc multimodal: {len(batches)} rule-batch calls "
        f"({len(page_pngs)} pages, {len(diagram_indices)} with diagrams, "
        f"{min(MAX_IMAGES_PER_CALL, len(page_pngs))} images per call), "
        f"concurrency={max_concurrent}"
    )

    # Extended thinking on multimodal path (default ON).
    # When enabled, Bedrock requires:
    #   - temperature=1.0
    #   - maxTokens > budget_tokens (otherwise ValidationException)
    # Falls back to temperature=0.0 + maxTokens=4000 when disabled.
    thinking_on = _thinking_enabled_multimodal()
    inference_temp = 1.0 if thinking_on else 0.0
    # When thinking is on, max_tokens must exceed budget_tokens. We
    # reserve ~4K for the actual JSON answer beyond the thinking budget.
    max_tokens = (THINKING_BUDGET_TOKENS + 4000) if thinking_on else 4000
    extra_request_kwargs = _thinking_request_fields() if thinking_on else {}

    async def _run_doc_batch(batch: list[dict]) -> tuple:
        msg = _build_whole_doc_message(
            page_texts=page_texts,
            page_pngs=page_pngs,
            diagram_page_indices=diagram_indices,
            rule_batch=batch,
            references_by_rule=references_by_rule,
            hosting_scope=declared_scope,
        )
        async with sem:
            t0 = _time.monotonic()
            try:
                resp = await _asyncio.to_thread(
                    client.converse,
                    modelId=model_id,
                    messages=[msg],
                    inferenceConfig={"maxTokens": max_tokens, "temperature": inference_temp},
                    **extra_request_kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"whole-doc batch failed: {exc}")
                return [], msg["content"][0]["text"], "", 0, 0, 0, "", None
        latency_ms = int((_time.monotonic() - t0) * 1000)
        usage = resp.get("usage") or {}
        in_tok = int(usage.get("inputTokens", 0))
        out_tok = int(usage.get("outputTokens", 0))
        blocks = resp.get("output", {}).get("message", {}).get("content", [])
        raw_text = "".join(b.get("text", "") for b in blocks if "text" in b)
        reasoning_text, reasoning_signature = _extract_reasoning_blocks(blocks)
        verdicts = _extract_json_array(raw_text)
        if not verdicts and raw_text:
            logger.warning(
                f"whole-doc batch: model returned text but parser yielded "
                f"0 verdicts. raw[:300]={raw_text[:300]!r}"
            )
        return (
            verdicts,
            msg["content"][0]["text"],
            raw_text,
            in_tok,
            out_tok,
            latency_ms,
            reasoning_text,
            reasoning_signature,
        )

    tasks = [_asyncio.ensure_future(_run_doc_batch(batch)) for batch in batches]

    by_rule: dict[str, dict] = {}
    last_trace_per_rule: dict[str, dict] = {}
    bedrock_calls = 0
    in_tokens = 0
    out_tokens = 0

    # Cooperative cancellation — deferred import to avoid a circular import
    # at module load (parallel_validator imports this module).
    from app.services.parallel_validator import (
        is_cancel_requested,
        ValidationCancelled,
    )

    # Drain as_completed for incremental progress reporting.
    for fut in _asyncio.as_completed(tasks):
        # Cancellation checkpoint between batches.
        if is_cancel_requested(validation_id):
            for t in tasks:
                t.cancel()
            # Await cancelled tasks so they unwind cleanly before raising.
            await _asyncio.gather(*tasks, return_exceptions=True)
            logger.info(f"Multimodal validation {validation_id} cancelled by user")
            raise ValidationCancelled(validation_id)
        (
            verdicts,
            prompt_text,
            raw_text,
            in_tok,
            out_tok,
            latency_ms,
            reasoning_text,
            reasoning_signature,
        ) = await fut
        if raw_text or verdicts:
            bedrock_calls += 1
        in_tokens += in_tok
        out_tokens += out_tok

        # Build a doc-level chunk_excerpt for the trace (truncated to
        # ~1200 chars so DDB rows don't blow up). The chat panel uses
        # this as "what the model saw".
        doc_excerpt = "\n\n".join(
            f"[Page {i + 1}] {t[:200]}" for i, t in enumerate(page_texts) if t
        )[:1200]

        for v in verdicts:
            rid = v.get("rule_id")
            if not rid:
                continue
            status = _status_for(v.get("verdict", ""))
            prev = by_rule.get(rid)
            if prev and _rank(status) <= _rank(prev["status"]):
                continue
            by_rule[rid] = {
                "status": status,
                "evidence": v.get("evidence", ""),
                "recommendation": v.get("recommendation", ""),
                "components": v.get("affected_components", []) or [],
                "page_number": 0,
            }
            # Trace carries reasoning, model_id, and the opaque
            # reasoning_signature (needed only for tool-use
            # round-tripping; harmless to ignore otherwise).
            last_trace_per_rule[rid] = {
                "prompt": prompt_text,
                "chunk_excerpt": doc_excerpt,
                "raw_response": raw_text[:2000],
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "latency_ms": latency_ms,
                "reasoning": reasoning_text or None,
                "reasoning_signature": reasoning_signature,
                "model_id": model_id,
            }

        if on_progress is not None:
            try:
                maybe = on_progress(len(by_rule), len(standards))
                if _asyncio.iscoroutine(maybe):
                    await maybe
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"on_progress failed: {exc}")

    # Post-loop cancellation check. The per-iteration checkpoint above only
    # fires between batches; a small doc with a single batch (one long
    # Bedrock call that can't be interrupted mid-flight) would finish the
    # loop before any checkpoint. Honour a cancel that arrived during that
    # in-flight call by discarding the result here rather than persisting a
    # completed run the user asked to stop.
    if is_cancel_requested(validation_id):
        logger.info(
            f"Multimodal validation {validation_id} cancelled by user (post-loop)"
        )
        raise ValidationCancelled(validation_id)

    # Build Finding objects. Iterate over ALL standards (not just
    # by_rule) so a rule omitted from every batch — or lost when a batch
    # errored with no retry — does NOT silently vanish. Missing rules
    # become explicit NOT_ADDRESSED findings (a coverage gap, excluded
    # from the score denominator) rather than disappearing, which had
    # made the evaluated_count silently under-report.
    findings: list[Finding] = []
    for std in standards:
        rid = std.get("id")
        if not rid:
            continue
        outcome = by_rule.get(rid)
        trace_dict = last_trace_per_rule.get(rid)
        if outcome is None:
            findings.append(
                Finding(
                    finding_id=f"find-{uuid.uuid4().hex[:8]}",
                    requirement_id=rid,
                    requirement_title=std.get("name", rid),
                    severity=_severity_for(std.get("severity", "medium")),
                    status=ComplianceStatus.NOT_ADDRESSED,
                    evidence="No verdict was returned for this rule.",
                    recommendation=str(std.get("description", ""))[:1000],
                )
            )
            continue
        findings.append(
            Finding(
                finding_id=f"find-{uuid.uuid4().hex[:8]}",
                requirement_id=rid,
                requirement_title=std.get("name", rid),
                severity=_severity_for(std.get("severity", "medium")),
                status=outcome["status"],
                evidence=outcome["evidence"][:2000],
                recommendation=outcome["recommendation"][:1000],
                affected_components=outcome["components"],
                source_section=f"Page {outcome['page_number']}",
                trace=trace_dict,
            )
        )

    # Adversarial verify pass for FAIL/critical findings — same accuracy
    # protection the chunked path gets, so the false-FAIL guard is consistent
    # across ingest paths (not just chunked-text PDFs). Runs BEFORE the
    # decision tree so a downgraded FAIL->WARNING is reflected in the rollup.
    # Verify tokens fold into the run's cost telemetry.
    from app.services.parallel_validator import (
        resolve_decision_tree,
        _calculate_score,
        _verify_findings_async,
    )
    std_by_id = {s.get("id"): s for s in standards if s.get("id")}
    v_calls, v_in, v_out = await _verify_findings_async(client, findings, std_by_id, sem)
    bedrock_calls += v_calls
    in_tokens += v_in
    out_tokens += v_out

    # Apply the founding/substitution/exception decision tree + layer /
    # architecture rollups HERE, over the tier/scope-filtered `standards`
    # this function actually evaluated. (Running it in the caller over the
    # UNfiltered superset produced wrong layer verdicts + dangling
    # founding_finding_ids for filtered-out rules.) Then score via the
    # shared _calculate_score (NOT_ADDRESSED / N/A excluded; None when
    # nothing evaluable).
    findings, layer_verdicts, architecture_verdict = resolve_decision_tree(
        findings, standards, tier_used=declared_tier
    )
    score = _calculate_score(findings)

    return ValidationResult(
        validation_id=validation_id,
        document_id=document_id,
        layer_verdicts=layer_verdicts,
        architecture_verdict=architecture_verdict,
        status=ValidationStatus.COMPLETED,
        standards_set=standards_set,
        started_at=started_at,
        completed_at=datetime.utcnow(),
        findings=findings,
        overall_score=score,
        summary=f"Multimodal validation: {len(findings)} verdicts across {len(page_pngs)} pages.",
        evaluated_count=len(findings),
        total_standards=len(standards),
        bedrock_calls=bedrock_calls,
        bedrock_input_tokens=in_tokens,
        bedrock_output_tokens=out_tokens,
    )


def validate_pdf_multimodal_document(
    *,
    pdf_bytes: bytes,
    page_texts: list[str],
    document_id: str,
    standards: list[dict],
    declared_tier: Optional[int] = None,
    declared_scope: Optional[str] = None,
    standards_set: str = "default",
    on_progress=None,
) -> ValidationResult:
    """Sync wrapper around the async validator. Tests and the
    parallel_validator dispatcher both use this entry point. The
    dispatcher already runs us inside asyncio.to_thread so the inner
    asyncio.run is fine."""
    import asyncio as _asyncio

    return _asyncio.run(
        _validate_pdf_multimodal_async(
            pdf_bytes=pdf_bytes,
            page_texts=page_texts,
            document_id=document_id,
            standards=standards,
            declared_tier=declared_tier,
            declared_scope=declared_scope,
            standards_set=standards_set,
            on_progress=on_progress,
        )
    )
