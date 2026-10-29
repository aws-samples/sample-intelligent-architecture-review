# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unified multimodal extraction agent + parallel driver.

Replaces the two divergent extractors (extraction_agent.py for PDF text-only,
extraction_agent_pptx.py for PPTX multimodal) with ONE agent that runs per
Unit, sees text + structured tables + full-page image together, and is bound
to a per-run ExtractionContext (no module globals) so units run in PARALLEL.

Driver shape mirrors the validator's proven concurrency model
(asyncio.Semaphore + gather) rather than a serial for-loop. Failure is
isolated per unit — one bad unit doesn't sink the run.
"""

from __future__ import annotations

import asyncio
import logging
import os

from strands import Agent
from strands.models import BedrockModel

from app.agents.extraction.context import (
    ExtractionContext,
    make_save_standard,
    make_save_reference,
)
from app.agents.extraction.multimodal import (
    UNIT_MODEL_ID,
    build_unit_message,
    resolve_image_bytes,
)
from app.agents.extraction.unit import Unit
from app.bedrock_models import OPUS_VISION, model_for
from app.services.bedrock_guardrail import strands_guardrail_kwargs

logger = logging.getLogger(__name__)

# Concurrency ceiling for parallel unit extraction. Lower than the
# validator's 25 because each unit-agent makes several Converse calls in its
# loop and Sonnet's TPM/RPM is the binding constraint. Env-tunable.
EXTRACTION_MAX_CONCURRENT = int(os.getenv("EXTRACTION_MAX_CONCURRENT", "8"))

# Diagram units run on Opus (topology analysis + rule extraction),
# which has tighter TPM/RPM and higher cost/latency than Sonnet, so they get a
# SEPARATE, lower concurrency ceiling. Text/table units keep the Sonnet ceiling
# above. A run interleaves both under their own semaphores.
EXTRACTION_DIAGRAM_MAX_CONCURRENT = int(os.getenv("EXTRACTION_DIAGRAM_MAX_CONCURRENT", "3"))

# Diagram rule-extraction model — Opus (verified in 577). Falls back to the
# Sonnet unit model if unset. Kept in sync with topology.DIAGRAM_MODEL_ID.
DIAGRAM_UNIT_MODEL_ID = model_for("EXTRACTION_DIAGRAM_MODEL_ID", OPUS_VISION)

_SYSTEM_PROMPT = """You are an enterprise High Availability (HA) Standards Extractor.

You are given ONE unit (a slide or a page) of an infrastructure standards or
System Design Document. Extract every HA validation requirement it states or
implies, and save each via the save_standard tool.

You receive, together, in one message:
  - the unit's NATIVE TEXT (authoritative for rule wording — quote verbatim in source_quote),
  - its TABLES rendered as real markdown grids (a tier×layer matrix: EACH cell
    with content is a separate rule for THAT tier and THAT layer — do not
    collapse rows), and
  - the full-page IMAGE (read diagrams for HA topology the text doesn't state:
    redundancy, failover, replication, DR sites, load balancing).

Rule schema (mandatory): name, description, validation_criteria, severity
(critical|high|medium|low|info), category, applies_to_tiers (list 1..5; use
[1,2,3,4,5] for universal), layer (datacenter|data|web|application|
load_balancer|general), rule_type (founding|substitution|exception),
source_quote (verbatim when text is present), page_reference (use the unit
label given), hosting_scope="{{HOSTING_SCOPE}}" on EVERY call.

References vs rules: if the unit states FACTUAL CONTEXT that supports rule
judgments but is not itself testable — datacenter/DR-site peer maps, network
zone catalogs, approved-technology lists, glossary/tier-definition tables —
save it via save_reference (NOT save_standard). A rule is something an SDD is
judged against; a Reference is context the validator reads.

Rules:
- Tier×layer matrix cell → one founding rule per (tier, layer). Never collapse
  a 5-row matrix into one universal rule.
- If this unit is an empty form template / instruction placeholder (e.g. "fill
  out the table for each application", blank cells), extract NOTHING and reply
  with one short sentence saying so. Do NOT turn instructions into rules.
- Never invent rules unsupported by the unit. Never paraphrase when verbatim
  text exists.
"""


def _build_agent(ctx: ExtractionContext, unit: Unit,
                 model_id: str | None = None) -> Agent:
    """One fresh, context-bound multimodal agent for a single unit.

    ``model_id`` overrides the default Sonnet unit model — diagram units pass
    the Opus id. No ``temperature`` is set: BedrockModel omits it when unset,
    which is required for Opus (temperature is deprecated there)."""
    model = BedrockModel(
        model_id=model_id or UNIT_MODEL_ID,
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        max_tokens=8000,
        # Strands takes flat guardrail_* keywords rather than the Converse
        # guardrailConfig shape, so this path cannot reuse the client wrapper.
        **strands_guardrail_kwargs(),
    )
    tools = [
        make_save_standard(ctx, unit_label=unit.label),
        make_save_reference(ctx, unit_label=unit.label),
    ]

    # Callback handler: feed the live-log (frontend progress banner) rather
    # than Strands' default PrintingCallbackHandler, which floods stdout/
    # CloudWatch with every agent's full response. Falls back to the silent
    # null handler if the live-log handler is unavailable.
    callback = None
    try:
        from app.services.extraction_log import LiveLogCallbackHandler
        if ctx.document_id:
            callback = LiveLogCallbackHandler(ctx.document_id)
    except Exception:
        callback = None
    if callback is None:
        try:
            from strands.handlers.callback_handler import null_callback_handler
            callback = null_callback_handler
        except Exception:
            callback = None

    kwargs = {
        "model": model,
        "system_prompt": _SYSTEM_PROMPT.replace("{{HOSTING_SCOPE}}", ctx.hosting_scope),
        "tools": tools,
    }
    if callback is not None:
        kwargs["callback_handler"] = callback
    return Agent(**kwargs)


def _extract_one_unit_sync(ctx: ExtractionContext, unit: Unit, doc_store,
                           preamble: str | None) -> dict:
    """Blocking: build + run one unit-agent. Returns a small status dict.

    Diagram units: first run the topology extract + self-consistency
    verify pass on Opus, stash the verified graph on the context (so the bound
    save_standard tags every resulting rule with topology_confidence /
    topology_graph / needs_review), then run rule extraction ALSO on Opus with
    the verified graph injected into the prompt. Text/table units keep the
    Sonnet path unchanged. Topology is fully non-fatal — a failure degrades the
    unit to the plain multimodal path.
    """
    image_bytes = resolve_image_bytes(unit, doc_store)

    topology_block: str | None = None
    model_id: str | None = None  # None → default Sonnet unit model
    if unit.is_diagram and image_bytes:
        model_id = DIAGRAM_UNIT_MODEL_ID
        try:
            from app.agents.extraction.topology import analyze_diagram
            analysis = analyze_diagram(image_bytes)
            if analysis is not None:
                ctx.topology_by_unit[unit.label] = {
                    "confidence": analysis.confidence,
                    "graph": analysis.graph,
                }
                topology_block = analysis.to_prompt_block()
                try:
                    from app.services.extraction_log import append as _log
                    _log(ctx.document_id, "info",
                         f"{unit.label}: diagram topology {analysis.confidence}-confidence "
                         f"({len(analysis.graph.get('nodes', []))} nodes, "
                         f"{len(analysis.graph.get('edges', []))} edges"
                         + (f", {len(analysis.discrepancies)} discrepancy flag(s)"
                            if analysis.discrepancies else "") + ")")
                except Exception:
                    pass
        except Exception:
            logger.warning(f"{unit.label}: topology analysis failed; plain multimodal",
                           exc_info=True)

    agent = _build_agent(ctx, unit, model_id=model_id)
    message = build_unit_message(unit, image_bytes, preamble=preamble,
                                 topology_block=topology_block)
    before = len(ctx.rules_by_unit.get(unit.label, []))
    agent(message)
    saved = len(ctx.rules_by_unit.get(unit.label, [])) - before
    return {"unit": unit.label, "rules": saved, "had_image": image_bytes is not None,
            "diagram": bool(model_id)}


async def extract_units(ctx: ExtractionContext, units: list[Unit], doc_store,
                        on_progress=None) -> dict:
    """Run extraction across all units IN PARALLEL under a semaphore.

    Each unit runs its blocking agent in a worker thread (asyncio.to_thread)
    so the event loop stays free; the semaphore caps concurrent Bedrock
    pressure. Per-unit failures are captured, not raised.
    """
    # Two ceilings: text/table units on the Sonnet semaphore (wide), diagram
    # units on the Opus semaphore (narrow, tighter quota). A diagram unit
    # takes BOTH conceptually but we gate it on the diagram sem only — the
    # binding constraint is Opus TPM/RPM, and diagram units are the minority.
    sem = asyncio.Semaphore(EXTRACTION_MAX_CONCURRENT)
    diagram_sem = asyncio.Semaphore(EXTRACTION_DIAGRAM_MAX_CONCURRENT)
    total = len(units)
    done = 0
    results: list[dict] = []
    failures: list[str] = []

    # Preambles: a title/section unit orients the ones that follow. We pass
    # the immediately-preceding low-content unit's text as orientation.
    preambles: dict[int, str | None] = {}
    prev_title: str | None = None
    for u in units:
        preambles[u.index] = prev_title
        if u.classification in ("title_only", "pdf_section") and len(u.text) < 120:
            prev_title = u.text
        else:
            prev_title = None

    async def run_unit(unit: Unit):
        nonlocal done
        # Diagram units run on Opus → gate them on the narrower diagram
        # semaphore; everything else on the Sonnet semaphore.
        use_sem = diagram_sem if (unit.is_diagram) else sem
        async with use_sem:
            try:
                res = await asyncio.to_thread(
                    _extract_one_unit_sync, ctx, unit, doc_store, preambles.get(unit.index)
                )
                results.append(res)
            except Exception as exc:  # noqa: BLE001 — isolate per-unit failure
                failures.append(unit.label)
                logger.warning(f"{unit.label} extraction failed: {exc}", exc_info=True)
                try:
                    from app.services.extraction_log import append as _log
                    _log(ctx.document_id, "warning", f"{unit.label} failed: {exc}")
                except Exception:
                    pass
            finally:
                done += 1
                if on_progress is not None:
                    try:
                        maybe = on_progress(done, total)
                        if asyncio.iscoroutine(maybe):
                            await maybe
                    except Exception:
                        pass

    await asyncio.gather(*[run_unit(u) for u in units])

    rules_saved = sum(len(v) for v in ctx.rules_by_unit.values())
    return {
        "units_total": total,
        "units_failed": len(failures),
        "failed_units": failures,
        "rules_saved": rules_saved,
        "per_unit": results,
    }
