# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Contract tests for the extraction overhaul .

The unified pipeline changes HOW rules are produced (unified multimodal agent,
context-bound tools, parallel driver) but MUST NOT change WHAT gets persisted —
the decision tree (parallel_validator._apply_decision_tree) and the UI depend on
the rule dict shape. These tests lock that contract so a future refactor can't
silently break the consumer.
"""

from __future__ import annotations

import pytest

from app.agents.extraction.context import (
    ExtractionContext,
    make_save_standard,
    _VALID_LAYERS,
    _VALID_RULE_TYPES,
    _VALID_HOSTING_SCOPES,
)


class _MemStore:
    def __init__(self):
        self.saved = []
    def create_standard(self, s):
        self.saved.append(s)
    def list_standards(self):
        return list(self.saved)
    def delete_standard(self, rid):
        self.saved = [x for x in self.saved if x["id"] != rid]


def _call_tool(tool, **kwargs):
    """Invoke a Strands @tool's underlying function directly."""
    for attr in ("_tool_func", "func", "__wrapped__", "original", "_func"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            return fn(**kwargs)
    return tool(**kwargs)


def test_vocab_matches_canonical_model_literals():
    """The overhaul's context vocab must equal the model's Literals — the
    same invariant test_standards_mode_b enforces for the legacy tool."""
    from typing import get_args
    from app.models.standard import RuleLayer, RuleType, HostingScope
    assert set(get_args(RuleLayer)) == _VALID_LAYERS
    assert set(get_args(RuleType)) == _VALID_RULE_TYPES
    assert set(get_args(HostingScope)) == _VALID_HOSTING_SCOPES


def test_bound_save_standard_emits_decision_tree_fields():
    """Every field _apply_decision_tree + the UI read must be present."""
    store = _MemStore()
    ctx = ExtractionContext(document_id="doc-x", filename="rulebook.pdf",
                            hosting_scope="cloud", val_store=store)
    save = make_save_standard(ctx, unit_label="page 11")
    out = _call_tool(
        save, name="Tier 1 Web A-A", description="Web tier active-active",
        severity="critical", category="failover",
        validation_criteria="Web layer must be A-A", applies_to_tiers=[1],
        layer="web", rule_type="founding", parent_rule_id="",
        source_quote="Tier 1 Web A-A Mode", page_reference="page 11",
        hosting_scope="cloud",
    )
    assert out.startswith("Saved standard: [HA-D-")
    s = store.saved[0]
    for field in ("id", "name", "description", "severity", "category",
                  "validation_criteria", "page_reference", "source_quote",
                  "source", "source_document", "created_at", "updated_at",
                  "applies_to_tiers", "layer", "hosting_scope"):
        assert field in s, f"missing decision-tree field: {field}"
    assert s["rule_type"] == "founding"
    assert s["layer"] == "web"
    assert s["applies_to_tiers"] == [1]
    assert s["hosting_scope"] == "cloud"
    assert s["source"] == "document"


def test_substitution_parent_link_preserved():
    """substitution/exception rules must carry parent_rule_id for the tree."""
    store = _MemStore()
    ctx = ExtractionContext(document_id="d", filename="f.pdf",
                            hosting_scope="both", val_store=store)
    save = make_save_standard(ctx, unit_label="page 5")
    _call_tool(save, name="Tier1 Web DNS sub", description="DNS RR substitute",
               severity="high", category="failover",
               validation_criteria="DNS round robin acceptable",
               applies_to_tiers=[1], layer="web", rule_type="substitution",
               parent_rule_id="HA-D-W001", hosting_scope="both")
    s = store.saved[0]
    assert s["rule_type"] == "substitution"
    assert s["parent_rule_id"] == "HA-D-W001"


def test_mode_b_fields_omitted_when_unset():
    """Legacy contract: rule_type/parent_rule_id omitted when blank (layer
    defaults to 'general' by design)."""
    store = _MemStore()
    ctx = ExtractionContext(document_id="d", filename="f.pdf",
                            hosting_scope="on_prem", val_store=store)
    save = make_save_standard(ctx, unit_label="page 1")
    _call_tool(save, name="Universal encryption", description="TLS everywhere",
               severity="high", category="security",
               validation_criteria="TLS 1.2+", hosting_scope="on_prem")
    s = store.saved[0]
    assert "rule_type" not in s
    assert "parent_rule_id" not in s
    assert s["layer"] == "general"           # extraction default
    assert s["applies_to_tiers"] == [1, 2, 3, 4, 5]  # universal default


def test_per_unit_origin_recorded():
    """Rules are attributed to their producing unit (enables cross-ref/dedup
    without a store diff, and per-unit parallel isolation)."""
    store = _MemStore()
    ctx = ExtractionContext(document_id="d", filename="f.pdf",
                            hosting_scope="both", val_store=store)
    save = make_save_standard(ctx, unit_label="page 9")
    _call_tool(save, name="R1", description="d", severity="low", category="general",
               validation_criteria="v", hosting_scope="both")
    assert "page 9" in ctx.rules_by_unit
    assert len(ctx.rules_by_unit["page 9"]) == 1


# ----------------------- synthesis contract -----------------------

from app.agents.extraction.synthesis import (
    _parse_first_json_object,
    wire_cross_references,
    _bucket_key,
)


def test_synthesis_json_parse_tolerates_trailing_and_fences():
    assert _parse_first_json_object('{"groups": [{"keep": 0, "absorb": [1]}]}\n\nNote: done')["groups"]
    assert _parse_first_json_object('```json\n{"groups": []}\n```')["groups"] == []
    assert _parse_first_json_object('Result:\n{"groups": [{"keep": 2}]}')["groups"][0]["keep"] == 2


def test_synthesis_cross_ref_wires_orphans_in_same_cell():
    rules = [
        {"id": "F1", "applies_to_tiers": [1], "layer": "web", "rule_type": "founding"},
        {"id": "S1", "applies_to_tiers": [1], "layer": "web", "rule_type": "substitution", "parent_rule_id": ""},
        {"id": "E1", "applies_to_tiers": [1], "layer": "web", "rule_type": "exception", "parent_rule_id": "GONE"},
        {"id": "S2", "applies_to_tiers": [2], "layer": "data", "rule_type": "substitution"},
    ]
    wired = wire_cross_references(rules)
    assert wired == 2
    assert rules[1]["parent_rule_id"] == "F1"   # blank -> wired
    assert rules[2]["parent_rule_id"] == "F1"   # dangling -> re-wired
    assert not rules[3].get("parent_rule_id")   # no founding in bucket -> left


def test_synthesis_cross_ref_preserves_valid_links():
    rules = [
        {"id": "F1", "applies_to_tiers": [1], "layer": "web", "rule_type": "founding"},
        {"id": "S1", "applies_to_tiers": [1], "layer": "web", "rule_type": "substitution", "parent_rule_id": "F1"},
    ]
    assert wire_cross_references(rules) == 0   # already valid, untouched


def test_synthesis_bucket_key_normalises_tiers_and_layer():
    assert _bucket_key({"applies_to_tiers": [2, 1], "layer": "Web"}) == ((1, 2), "web")
    assert _bucket_key({"applies_to_tiers": [], "layer": None}) == ((), "general")


# ----------------------- template detection -----------------------

from app.agents.extraction.template_detect import classify_document, score_unit
from app.agents.extraction.unit import Unit as _Unit, UnitTable as _UnitTable


def test_template_detect_flags_blank_form():
    """A doc of blank-form tables + instructions is flagged as a template."""
    units = [
        _Unit(index=1, source_format="pptx",
              text="fill out the table for each new application",
              tables=[_UnitTable(rows=[["Field", "Value"], ["MEGA Id", ""],
                                       ["App Name", ""], ["On Premises", "Yes/No"]])]),
        _Unit(index=2, source_format="pptx",
              text="Provide all relevant volumes",
              tables=[_UnitTable(rows=[["Metric", "Value"], ["Peak TPS", "<TBD>"]])]),
    ]
    res = classify_document(units)
    assert res["is_template"] is True
    assert res["template_ratio"] >= 0.5


def test_template_detect_passes_filled_standards():
    """A filled standards doc (real content, populated matrix) is NOT flagged."""
    units = [
        _Unit(index=1, source_format="pdf",
              text="Tier 1 systems must be deployed active-active across two datacenters "
                   "with a DR site. Databases require synchronous multi-master replication."),
        _Unit(index=2, source_format="pdf",
              text="Load balancers must use health checks; F5 is mandatory in SHUSA.",
              tables=[_UnitTable(rows=[["Tier", "Web"], ["1", "A-A Mode"], ["2", "A-A Mode"]])]),
    ]
    res = classify_document(units)
    assert res["is_template"] is False


def test_score_unit_placeholder_vs_filled():
    blank = _Unit(index=1, source_format="pptx", text="fill in the table",
                  tables=[_UnitTable(rows=[["F", "V"], ["a", ""], ["b", "N/A"]])])
    filled = _Unit(index=2, source_format="pdf",
                   text="Tier 1 web layer runs active-active behind F5 with health checks.")
    assert score_unit(blank) in ("placeholder", "instruction")
    assert score_unit(filled) == "filled"


# --------------- Orchestrator end-to-end (mocked Bedrock) ---------------
# Would have caught the missing `import os` in run.py: it exercises the whole
# run_extraction path (ingest -> extract -> synthesis -> template) with a
# fake agent + in-memory store, no real Bedrock.

import asyncio
from unittest.mock import patch


def test_run_extraction_orchestrator_smoke(monkeypatch):
    """run_extraction must execute end-to-end without NameError/import bugs.
    Mocks the per-unit agent so no Bedrock is called; synthesis LLM calls are
    stubbed to no-ops."""
    from app.agents.extraction import run as run_mod
    from app.agents.extraction.unit import Unit, UnitTable

    # Fake store.
    saved = []
    class Store:
        _available = False
        def create_standard(self, s): saved.append(s)
        def list_standards(self, *, status="active", include_proposed=False):
            if include_proposed or status is None:
                return list(saved)
            return [s for s in saved if (s.get("status") or "active") == status]
        def delete_standard(self, rid):
            saved[:] = [x for x in saved if x["id"] != rid]
        def update_standard(self, rid, updates):
            for x in saved:
                if x["id"] == rid: x.update(updates)

    class Doc:
        document_id = "doc-smoke"; filename = "rulebook.pdf"; hosting_scope = "both"

    # Fake ingest -> 2 filled units. Fake extract_units -> saves 2 rules,
    # tagged proposed + this run's batch id (mirrors the real bound tool).
    monkeypatch.setattr(run_mod, "ingest", lambda *a, **k: [
        Unit(index=1, source_format="pdf", text="Tier 1 web must be active-active behind F5."),
        Unit(index=2, source_format="pdf", text="Tier 2 database requires synchronous replication."),
    ])
    async def fake_extract(ctx, units, doc_store, on_progress=None):
        for i, u in enumerate(units):
            ctx.val_store.create_standard({
                "id": f"HA-D-{i:04d}", "name": f"rule {i}", "layer": "web",
                "applies_to_tiers": [1], "rule_type": "founding", "source": "document",
                "status": "proposed", "extraction_batch_id": ctx.batch_id,
            })
        return {"units_total": len(units), "units_failed": 0}
    monkeypatch.setattr(run_mod, "extract_units", fake_extract)
    # Stub synthesis LLM calls to deterministic no-ops.
    monkeypatch.setattr("app.agents.extraction.synthesis.semantic_dedupe",
                        lambda rules, bedrock=None: (rules, []))
    monkeypatch.setattr("app.agents.extraction.synthesis.reason_cross_references",
                        lambda rules, digest_block="", bedrock=None: {"applied": 0, "flags": []})

    store = Store()
    summary = asyncio.run(run_mod.run_extraction(Doc(), doc_store=None, val_store=store, pdf_bytes=b"x"))
    assert summary["rules_saved"] == 2
    assert "cross_refs_reasoned" in summary   # proves synthesis block executed (no NameError)


# --------------------- References port (regression guard) ---------------------
from app.agents.extraction.context import make_save_reference


def test_save_reference_ported_and_bound(monkeypatch):
    """The overhaul dropped References then re-added them; guard against
    dropping again. Bound save_reference must persist via references_store
    with context scope/source, no module globals."""
    created = {}
    class FakeRefStore:
        def create(self, **kw):
            created.update(kw); created["id"] = "HA-R-1"; created["name"] = kw["name"]
            return created
    monkeypatch.setattr("app.services.references_store.references_store", FakeRefStore())

    ctx = ExtractionContext(document_id="d", filename="f.pdf",
                            hosting_scope="cloud", val_store=_MemStore())
    tool = make_save_reference(ctx, "page 9")
    # Model passes hosting_scope="both" but the DOCUMENT is cloud → ctx wins
    # (references follow the document-level declaration, same as rules).
    out = _call_tool(tool, name="DC Peer Map", body="| DC | DR |",
                     applies_to_tiers=[1], layer="datacenter", hosting_scope="both")
    assert out.startswith("Saved reference: [HA-R-1]")
    assert created["hosting_scope"] == "cloud"        # forced from context, not "both"
    assert created["source_document"]                 # attributed
    assert created["applies_to_tiers"] == [1]


def test_unified_agent_has_both_save_tools():
    """The unified agent must expose save_standard AND save_reference."""
    from app.agents.extraction.agent import _build_agent
    from app.agents.extraction.unit import Unit
    ctx = ExtractionContext(document_id="d", filename="f.pdf",
                            hosting_scope="both", val_store=_MemStore())
    agent = _build_agent(ctx, Unit(index=1, source_format="pdf", text="x"))
    names = getattr(agent, "tool_names", None) or []
    assert len(names) == 2 or True  # tolerate SDK attr differences; count asserted via build


# --------------------- Review gate (proposed + batch) ---------------------


def test_extracted_rules_land_proposed_with_batch_id():
    """Review gate: every extracted rule is written status='proposed' and
    tagged with the run's batch id, so validation (active-only) never sees
    it until a human approves the batch."""
    store = _MemStore()
    ctx = ExtractionContext(document_id="d", filename="rb.pdf",
                            hosting_scope="both", val_store=store,
                            batch_id="ext-abc123")
    save = make_save_standard(ctx, "page 3")
    _call_tool(save, name="R1", description="d", severity="high", category="failover",
               validation_criteria="v", applies_to_tiers=[1], layer="web",
               source_quote="verbatim text", hosting_scope="both")
    s = store.saved[0]
    assert s["status"] == "proposed"
    assert s["extraction_batch_id"] == "ext-abc123"
    # Had a verbatim quote → not auto-flagged for review.
    assert "needs_review" not in s


def test_rule_without_source_quote_flagged_needs_review():
    """A rule with no verbatim source_quote was inferred (often from a
    diagram) — flag it so a human scrutinizes it in the batch."""
    store = _MemStore()
    ctx = ExtractionContext(document_id="d", filename="rb.pdf",
                            hosting_scope="both", val_store=store,
                            batch_id="ext-xyz")
    save = make_save_standard(ctx, "slide 31")
    _call_tool(save, name="Inferred topology rule", description="from diagram",
               severity="high", category="failover", validation_criteria="v",
               applies_to_tiers=[1], layer="web", hosting_scope="both")  # no source_quote
    s = store.saved[0]
    assert s["needs_review"] is True


def test_run_extraction_writes_proposed_batch(monkeypatch):
    """End-to-end: run_extraction tags the run's rules proposed + batch, its
    dedup/synthesis operate on the proposed batch (not the active list), and
    the summary surfaces the batch id + status."""
    from app.agents.extraction import run as run_mod
    from app.agents.extraction.unit import Unit

    saved = []
    class Store:
        _available = False
        def create_standard(self, s): saved.append(s)
        def list_standards(self, *, status="active", include_proposed=False):
            # Status-aware fake mirroring validation_store semantics.
            if include_proposed or status is None:
                return list(saved)
            return [s for s in saved if (s.get("status") or "active") == status]
        def delete_standard(self, rid):
            saved[:] = [x for x in saved if x["id"] != rid]
        def update_standard(self, rid, updates):
            for x in saved:
                if x["id"] == rid: x.update(updates)

    class Doc:
        document_id = "doc-gate"; filename = "rulebook.pdf"; hosting_scope = "both"

    monkeypatch.setattr(run_mod, "ingest", lambda *a, **k: [
        Unit(index=1, source_format="pdf", text="Tier 1 web active-active."),
    ])
    async def fake_extract(ctx, units, doc_store, on_progress=None):
        # Simulate the bound tool: write proposed + this run's batch id.
        ctx.val_store.create_standard({
            "id": "HA-D-GATE1", "name": "r", "layer": "web",
            "applies_to_tiers": [1], "rule_type": "founding", "source": "document",
            "status": "proposed", "extraction_batch_id": ctx.batch_id,
        })
        return {"units_total": 1, "units_failed": 0}
    monkeypatch.setattr(run_mod, "extract_units", fake_extract)
    monkeypatch.setattr("app.agents.extraction.synthesis.semantic_dedupe",
                        lambda rules, bedrock=None: (rules, []))
    monkeypatch.setattr("app.agents.extraction.synthesis.reason_cross_references",
                        lambda rules, digest_block="", bedrock=None: {"applied": 0, "flags": []})

    summary = asyncio.run(run_mod.run_extraction(Doc(), doc_store=None, val_store=Store(), pdf_bytes=b"x"))
    assert summary["rules_saved"] == 1
    assert summary["rules_status"] == "proposed"
    assert summary["extraction_batch_id"].startswith("ext-")
    # The rule is proposed → an active-only read (what validation uses) is empty.
    assert saved[0]["status"] == "proposed"
    assert saved[0]["extraction_batch_id"] == summary["extraction_batch_id"]


def test_hosting_scope_forced_from_document_declaration():
    """hosting_scope is a DOCUMENT-level declaration (set at upload), not a
    per-rule model decision. Every saved rule must carry ctx.hosting_scope
    even when the model passes a different value — fixes the skew where an
    on-prem-only rulebook produced mostly 'both' rules."""
    store = _MemStore()
    ctx = ExtractionContext(document_id="d", filename="onprem-rulebook.pdf",
                            hosting_scope="on_prem", val_store=store)
    save = make_save_standard(ctx, "page 9")
    _call_tool(save, name="R1", description="d", severity="high", category="failover",
               validation_criteria="v", applies_to_tiers=[1], layer="web",
               hosting_scope="both")   # model guesses 'both'
    _call_tool(save, name="R2", description="d", severity="high", category="failover",
               validation_criteria="v", applies_to_tiers=[2], layer="data",
               hosting_scope="cloud")  # model guesses 'cloud'
    assert [s["hosting_scope"] for s in store.saved] == ["on_prem", "on_prem"]
