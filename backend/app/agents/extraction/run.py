# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Extraction orchestrator.

Single entrypoint for both formats, replacing the format-divergent
orchestration in extraction_dispatch.run_pptx_extraction (PPTX) and the
chunked for-loop inside documents._trigger_extraction (PDF):

    ingest (unified Units)  ->  parallel multimodal extract  ->  dedup

Between extract and dedup:
  Stage 2: specialist fan-out per unit
  Stage 3: synthesis + cross-reference over the merged rule set

Preserves the dedup_rules key and the live-log contract. Returns a summary
dict compatible with what the callers logged before.
"""

from __future__ import annotations

import logging
import os

from app.agents.extraction.agent import extract_units
from app.agents.extraction.context import ExtractionContext
from app.agents.extraction.ingestion import ingest

logger = logging.getLogger(__name__)


async def run_extraction(document, doc_store, val_store, *,
                         pptx_bytes: bytes | None = None,
                         pdf_bytes: bytes | None = None,
                         on_progress=None) -> dict:
    """Ingest a document into Units, extract in parallel, dedup. Async."""
    from app.services.extraction_chunker import dedup_rules
    from app.services.extraction_log import append as log_append

    units = ingest(document, doc_store, pptx_bytes=pptx_bytes, pdf_bytes=pdf_bytes)
    log_append(
        document.document_id, "info",
        f"Unified extraction: {len(units)} units "
        f"({sum(1 for u in units if u.is_diagram)} diagram, "
        f"{sum(1 for u in units if u.has_tables)} with tables)",
    )

    # Template detection: a fill-in SDD template is a VALIDATION input, not
    # a standards source. If the doc is overwhelmingly instruction/
    # blank-form units, warn and skip extraction rather than manufacture junk
    # rules from form scaffolding. Individual instruction/placeholder units
    # are also skipped within a mixed doc.
    try:
        from app.agents.extraction.template_detect import classify_document
        tmpl = classify_document(units)
        if tmpl["is_template"] and os.getenv("ENABLE_TEMPLATE_GUARD", "1") == "1":
            log_append(
                document.document_id, "warning",
                f"Document looks like a fill-in template "
                f"({tmpl['template_ratio']:.0%} instruction/blank-form units), "
                f"not a standards source. Extraction skipped — upload the "
                f"standards/policy document instead.",
            )
            return {"units": len(units), "units_failed": 0, "rules_saved": 0,
                    "skipped_reason": "template_detected", **tmpl}
        # In a mixed doc, drop instruction/placeholder units before extraction.
        before = len(units)
        units = [u for u in units if u.content_kind == "filled"]
        if len(units) < before:
            log_append(document.document_id, "info",
                       f"Skipped {before - len(units)} instruction/placeholder unit(s)")
    except Exception:
        logger.exception("template detection failed; extracting all units")

    # Document-level context digest (glossary, section map, tier defs) — cheap,
    # deterministic. Fed to the cross-reference reasoning agent so it can
    # resolve references that span pages the per-unit agents never saw.
    digest_block = ""
    try:
        from app.agents.extraction.context_digest import build_digest
        digest_block = build_digest(units).to_prompt_block()
    except Exception:
        logger.exception("context digest build failed; continuing without it")

    # Review gate: mint a batch id for this run. Every rule extracted below is
    # written status="proposed" tagged with this id, so the doc-detail review
    # panel can group them and a human approves the batch before rules go
    # active (and before validation — which reads active-only — can see them).
    import uuid as _uuid
    batch_id = f"ext-{_uuid.uuid4().hex[:12]}"

    ctx = ExtractionContext(
        document_id=document.document_id,
        filename=document.filename,
        hosting_scope=document.hosting_scope,
        val_store=val_store,
        batch_id=batch_id,
    )

    # This run's rules are the proposed rows tagged with our batch id. We no
    # longer diff against the active library (rules land proposed now, so an
    # active snapshot would be empty for this run) — the batch id IS the
    # membership key for dedup/synthesis below.
    def _batch_rows() -> list[dict]:
        return [
            s for s in val_store.list_standards(status="proposed")
            if s.get("extraction_batch_id") == batch_id
        ]

    result = await extract_units(ctx, units, doc_store, on_progress=on_progress)

    # Notes-based References: parse a `## References` section out of the
    # user's extraction_notes and persist each as a Reference. Restores the
    # capability previously provided by _save_references_safe.
    # Non-fatal — extraction rules are already saved.
    try:
        notes = (getattr(document, "extraction_notes", None) or "").strip()
        if notes:
            from app.services.references_extractor import save_references_for_document
            ref_summary = save_references_for_document(
                document_id=document.document_id, extraction_notes=notes,
            )
            if ref_summary.get("created") or ref_summary.get("updated"):
                log_append(document.document_id, "info",
                           f"References (from notes): created={ref_summary['created']}, "
                           f"updated={ref_summary['updated']}")
    except Exception:
        logger.exception("notes-based references parsing failed; continuing")

    # Stage 1 — cheap exact dedup (same key as before: name, tiers, layer).
    this_run = _batch_rows()
    kept, dropped = dedup_rules(this_run)
    for rid in dropped:
        try:
            val_store.delete_standard(rid)
        except Exception:
            logger.warning(f"failed to delete duplicate {rid}", exc_info=True)

    # Stage 2 — semantic synthesis: LLM merges cross-page
    # restatements the exact key misses, then re-wires orphaned
    # substitution/exception rules to their founding parent. Flag-gated and
    # fully non-fatal — any failure leaves the exact-deduped set intact.
    sem_dropped: list[str] = []
    wired = 0
    xref: dict = {"applied": 0, "flags": []}
    if os.getenv("ENABLE_EXTRACTION_SYNTHESIS", "1") == "1":
        try:
            from app.agents.extraction.synthesis import (
                semantic_dedupe, wire_cross_references, reason_cross_references,
            )
            survivors = _batch_rows()
            merged, sem_dropped = semantic_dedupe(survivors)
            for rid in sem_dropped:
                try:
                    val_store.delete_standard(rid)
                except Exception:
                    logger.warning(f"failed to delete semantic-dup {rid}", exc_info=True)
            # Cross-reference: (1) deterministic same-cell heuristic as a cheap
            # baseline, then (2) LLM reasoning over the full set + digest to fix
            # cross-page parent links the heuristic can't see.
            wired = wire_cross_references(merged)
            xref = {"applied": 0, "flags": []}
            if os.getenv("ENABLE_XREF_REASONING", "1") == "1":
                xref = reason_cross_references(merged, digest_block=digest_block)
            # Persist parent links (heuristic + reasoning both mutated `merged`).
            if wired or xref.get("applied"):
                for r in merged:
                    try:
                        val_store.update_standard(r["id"], {"parent_rule_id": r.get("parent_rule_id")})
                    except Exception:
                        pass
            log_append(document.document_id, "info",
                       f"Synthesis: merged {len(sem_dropped)} semantic dup(s); "
                       f"wired {wired} heuristic + {xref.get('applied', 0)} reasoned "
                       f"cross-reference(s); {len(xref.get('flags', []))} flag(s)")
            # References from this document get the same treatment: parallel
            # units each save the context they rely on, so the same tier table
            # or notation legend arrives several times.
            from app.agents.extraction.synthesis import dedupe_references
            from app.services.references_store import references_store
            source = f"({document.document_id})"
            doc_refs = [
                r for r in references_store.list_all(status=None)
                if source in (r.get("source_document") or "")
            ]
            ref_dropped = dedupe_references(doc_refs)
            for ref_id in ref_dropped:
                try:
                    references_store.delete(ref_id)
                except Exception:
                    logger.warning(f"failed to delete duplicate reference {ref_id}", exc_info=True)
            if ref_dropped:
                log_append(document.document_id, "info",
                           f"Synthesis: merged {len(ref_dropped)} duplicate reference(s); "
                           f"{len(doc_refs) - len(ref_dropped)} kept")
        except Exception:
            logger.exception("synthesis stage failed; keeping exact-deduped set")

    final_rows = _batch_rows()
    final_count = len(final_rows)
    needs_review_count = sum(1 for s in final_rows if s.get("needs_review"))

    # Scope sanity check. hosting_scope is declared at upload and FORCED onto
    # every rule above, so a wrong selection (a cloud reference architecture
    # uploaded as on_prem) silently tags every rule on_prem with nothing to
    # signal it. Infer the scope from the content and, if it confidently
    # contradicts the declaration, stamp the inferred verdict on each proposed
    # rule so the review panel can warn. Purely advisory — never blocks, never
    # rewrites hosting_scope. Best-effort: any failure leaves the batch intact.
    if final_rows and os.getenv("ENABLE_SCOPE_CHECK", "1") == "1":
        try:
            from app.services.scope_classifier import detect_scope_mismatch
            sample = "\n\n".join(
                f"[{u.label}] {u.text}" for u in units if (u.text or "").strip()
            )
            mismatch = detect_scope_mismatch(document.hosting_scope, sample)
            if mismatch:
                for r in final_rows:
                    try:
                        val_store.update_standard(r["id"], {"content_scope": mismatch})
                    except Exception:
                        logger.warning(f"failed to stamp scope verdict on {r['id']}",
                                       exc_info=True)
                log_append(
                    document.document_id, "warning",
                    f"Declared scope '{mismatch['declared']}' but the document reads "
                    f"as '{mismatch['inferred']}' ({mismatch['confidence']} confidence). "
                    f"All {len(final_rows)} proposed rule(s) are tagged "
                    f"'{mismatch['declared']}' — review the scope before approving.",
                )
        except Exception:
            logger.exception("scope check failed; continuing without it")
        final_rows = _batch_rows()
    summary = {
        "units": result["units_total"],
        "units_failed": result["units_failed"],
        "rules_saved": final_count,
        "rules_dropped_dedup": len(dropped),
        "rules_dropped_semantic": len(sem_dropped),
        "cross_refs_wired": wired,
        "cross_refs_reasoned": xref.get("applied", 0),
        "xref_flags": xref.get("flags", []),
        # Review gate: the batch these proposed rules belong to, and how many
        # are flagged for human scrutiny. The doc-detail panel reads this.
        "extraction_batch_id": batch_id,
        "rules_needs_review": needs_review_count,
        "rules_status": "proposed",
    }
    log_append(
        document.document_id, "info",
        f"Unified extraction complete: units={summary['units']}, "
        f"failed={summary['units_failed']}, rules={summary['rules_saved']}, "
        f"dropped_dedup={summary['rules_dropped_dedup']}",
    )
    return summary
