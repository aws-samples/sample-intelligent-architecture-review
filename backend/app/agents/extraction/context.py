# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-run extraction context + context-bound tools.

The older extraction path stashed the active document in MODULE GLOBALS
(`_current_source_document` / `_current_document_id` in standards_tools.py).
That is exactly what made parallel extraction unsafe — two concurrent agents
would clobber each other's globals. Removing it unblocks both cross-reference
reasoning AND parallelism (the latency win).

Instead, each agent gets an `ExtractionContext` and a set of tools *bound to
it* via a factory. No shared mutable global; N agents can run concurrently,
each writing rules attributed to its own unit.

The bound `save_standard` preserves the EXACT persisted dict shape the
decision tree consumes — this module changes *how* context reaches the tool,
not *what* gets saved.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Optional


@dataclass
class ExtractionContext:
    """Everything a unit-agent needs to attribute and persist its output,
    without touching module state. One per extraction run; shared read-only
    across the run's agents, with a lock around the rule sink for the brief
    window where parallel agents append."""

    document_id: str
    filename: str
    hosting_scope: str
    val_store: object                       # ValidationStore (duck-typed for eval)
    # Review gate: every rule this run extracts is written status="proposed"
    # and tagged with this batch id so the doc-detail review panel can group
    # them and a human can approve/reject the batch before rules go active.
    batch_id: str = ""
    source_document: str = ""
    # Rules saved this run, tagged with the unit that produced them, so
    # cross-reference/dedup can reason about origin without a store diff.
    rules_by_unit: dict = field(default_factory=dict)
    # Per-unit diagram topology metadata, keyed by unit label. Set by
    # the driver before a diagram unit's agent runs; the bound save_standard
    # reads it so every rule that unit produces carries topology_confidence /
    # topology_graph / needs_review. Empty for text/table units.
    topology_by_unit: dict = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def __post_init__(self):
        if not self.source_document:
            self.source_document = f"{self.filename} ({self.document_id})"

    def record(self, unit_label: str, rule_id: str) -> None:
        with self._lock:
            self.rules_by_unit.setdefault(unit_label, []).append(rule_id)


# Vocab — mirror standards_tools (kept byte-equal by test_standards_mode_b).
_VALID_LAYERS = {"datacenter", "data", "web", "application", "load_balancer", "general"}
_VALID_RULE_TYPES = {"founding", "substitution", "exception"}
_VALID_HOSTING_SCOPES = {"on_prem", "cloud", "both"}


def _norm_enum(value: str, allowed: set) -> Optional[str]:
    if not value:
        return None
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    return cleaned if cleaned in allowed else None


def make_save_reference(ctx: ExtractionContext, unit_label: str = ""):
    """Return a `save_reference` tool bound to `ctx` (no module globals).

    Covers the agent-discovered References capability: factual context blocks
    (DC peer maps, network zones, approved-tech catalogs, glossaries) that are
    NOT testable rules but that the validator pulls into its REFERENCES block.
    Persists via references_store with the same shape the unbound
    save_reference tool produces.
    """
    from strands.tools import tool

    @tool
    def save_reference(
        name: str,
        body: str,
        applies_to_tiers: list = None,
        layer: str = "",
        hosting_scope: str = "",
        page_reference: str = "",
        source_quote: str = "",
    ) -> str:
        """Save a Reference — a factual context block (table, peer map, zone
        catalog, glossary), NOT a testable rule. Use for material that
        SUPPORTS rule judgments but that an SDD can't pass/fail. Rules go
        through save_standard instead. The validator prepends matching
        References (by tier/layer/scope) to each rule's prompt."""
        from app.services.references_store import references_store

        norm_layer = _norm_enum(layer, _VALID_LAYERS) or None
        # hosting_scope is a DOCUMENT-level declaration — force it from the
        # context, same as save_standard. A reference extracted from an on-prem
        # rulebook is on-prem context; letting it default to "both" would leak
        # it into cloud validations once a cloud rulebook exists (the validator
        # attaches a ref when ref.scope == "both" OR == rule.scope).
        norm_scope = _norm_enum(ctx.hosting_scope, _VALID_HOSTING_SCOPES) \
            or _norm_enum(hosting_scope, _VALID_HOSTING_SCOPES) or "both"
        tiers: list[int] = []
        for t in (applies_to_tiers or []):
            try:
                tiers.append(int(t))
            except (TypeError, ValueError):
                continue
        if not tiers:
            tiers = [1, 2, 3, 4, 5]

        item = references_store.create(
            name=name.strip(),
            body=body,
            applies_to_tiers=tiers,
            layer=norm_layer,
            hosting_scope=norm_scope,
            source_document=ctx.source_document,
            page_reference=page_reference or None,
            source_quote=source_quote or None,
        )
        result = f"Saved reference: [{item['id']}] {item['name']}"
        if ctx.document_id:
            try:
                from app.services.extraction_log import append as _log_append
                _log_append(ctx.document_id, "rule", result)
            except Exception:
                pass
        return result

    return save_reference


def make_save_standard(ctx: ExtractionContext, unit_label: str = ""):
    """Return a `save_standard` tool bound to `ctx` (no module globals).

    The persisted dict is byte-for-byte the shape validation_store expects
    and the decision tree consumes: id, name, description, severity, category,
    validation_criteria, page_reference, source_quote, source, source_document,
    created_at, updated_at, applies_to_tiers, [alternative_criteria],
    [rule_type], layer, [parent_rule_id], hosting_scope.
    """
    from strands.tools import tool

    @tool
    def save_standard(
        name: str,
        description: str,
        severity: str,
        category: str,
        validation_criteria: str,
        page_reference: str = "",
        applies_to_tiers: list = None,
        alternative_criteria: str = "",
        source_quote: str = "",
        rule_type: str = "",
        layer: str = "",
        parent_rule_id: str = "",
        hosting_scope: str = "",
    ) -> str:
        """Save one HA validation standard extracted from this unit. Additive
        to the golden set. hosting_scope is REQUIRED (on_prem|cloud|both) and
        must match the document's declared scope."""
        tiers: list = [1, 2, 3, 4, 5]
        if applies_to_tiers:
            try:
                tiers = sorted({int(t) for t in applies_to_tiers if 1 <= int(t) <= 5})
                if not tiers:
                    tiers = [1, 2, 3, 4, 5]
            except (TypeError, ValueError):
                tiers = [1, 2, 3, 4, 5]

        quote = (source_quote or "").strip()
        if len(quote) > 500:
            quote = quote[:497] + "…"

        layer_norm = _norm_enum(layer, _VALID_LAYERS) or "general"
        rule_type_norm = _norm_enum(rule_type, _VALID_RULE_TYPES)
        parent_norm = (parent_rule_id or "").strip() or None

        # hosting_scope is a DOCUMENT-LEVEL declaration made by the user at
        # upload time — it is NOT a per-rule decision. Force every rule to the
        # document's declared scope and ignore whatever the model passed. This
        # fixes the skew where an on-prem-only rulebook produced mostly "both"
        # rules because the agent guessed per-rule. The `hosting_scope` arg is
        # kept in the signature (the prompt still tells the model to send it)
        # but the context value always wins.
        scope_norm = _norm_enum(ctx.hosting_scope, _VALID_HOSTING_SCOPES) \
            or _norm_enum(hosting_scope, _VALID_HOSTING_SCOPES) or "both"

        now = datetime.now(timezone.utc).isoformat()
        standard = {
            "id": f"HA-D-{uuid.uuid4().hex[:8].upper()}",
            "name": name,
            "description": description,
            "severity": severity,
            "category": category,
            "validation_criteria": validation_criteria,
            "page_reference": page_reference,
            "source_quote": quote,
            "source": "document",
            "source_document": ctx.source_document,
            "created_at": now,
            "updated_at": now,
            "applies_to_tiers": tiers,
            "layer": layer_norm,
            "hosting_scope": scope_norm,
            # Review gate: extracted rules land as "proposed", grouped by the
            # run's batch id. Validation reads active-only, so a proposed rule
            # never enters a validation until a human approves the batch on the
            # document-detail review panel. This is the security lever that
            # stops an unreviewed (or misread-diagram) rule going live.
            "status": "proposed",
        }
        if ctx.batch_id:
            standard["extraction_batch_id"] = ctx.batch_id
        # Flag rules a human should scrutinize first. A rule with no verbatim
        # source_quote was inferred by the model (often from a diagram's
        # pixels) rather than lifted from text — exactly the case where a
        # misread is most likely.
        needs_review = not quote
        # Diagram topology. If this unit's diagram was analysed, the
        # driver stashed the verified graph + confidence here. Attach them to
        # every rule the unit produces, and force review whenever the self-
        # consistency pass wasn't high-confidence (the two readings disagreed,
        # or verification failed) — the precise case a diagram misread hides in.
        topo = ctx.topology_by_unit.get(unit_label)
        if topo:
            conf = topo.get("confidence")
            if conf:
                standard["topology_confidence"] = conf
            if topo.get("graph"):
                standard["topology_graph"] = topo["graph"]
            if conf and conf != "high":
                needs_review = True
        if needs_review:
            standard["needs_review"] = True
        if alternative_criteria:
            standard["alternative_criteria"] = alternative_criteria
        if rule_type_norm:
            standard["rule_type"] = rule_type_norm
        if parent_norm:
            standard["parent_rule_id"] = parent_norm

        ctx.val_store.create_standard(standard)
        ctx.record(unit_label, standard["id"])

        tiers_str = "all tiers" if len(tiers) == 5 else f"tier{'s' if len(tiers) > 1 else ''} {tiers}"
        result = f"Saved standard: [{standard['id']}] {name} ({tiers_str})"

        # Live-log (best-effort) — keep the frontend banner working.
        if ctx.document_id:
            try:
                from app.services.extraction_log import append as _log_append
                _log_append(ctx.document_id, "rule", result)
            except Exception:
                pass
        return result

    return save_standard
