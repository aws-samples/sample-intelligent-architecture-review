# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Diagram topology extraction + self-consistency verify .

Two layers, both offline (fake Bedrock):
  1. topology.analyze_diagram — extract → verify composition, confidence
     gating, and non-fatal degradation on malformed / failed calls.
  2. context.make_save_standard — topology metadata (confidence + graph)
     threads onto every rule a diagram unit produces, and confidence≠high
     forces needs_review.
"""

from __future__ import annotations

import app.agents.extraction.topology as T
from app.agents.extraction.context import ExtractionContext, make_save_standard


class _FakeClient:
    """converse() replays a scripted list of text responses in order."""
    def __init__(self, responses):
        self._r = list(responses)
        self.calls = 0

    def converse(self, **kw):
        txt = self._r[self.calls] if self.calls < len(self._r) else "{}"
        self.calls += 1
        return {"output": {"message": {"content": [{"text": txt}]}}}


_GRAPH = ('{"nodes":[{"id":"web","label":"Web","type":"server"},'
          '{"id":"db","label":"DB","type":"database"}],'
          '"edges":[{"src":"web","dst":"db","kind":"data_flow"}],'
          '"ha_attributes":{"redundancy":"active-active","dr":"yes"},"notes":"AA"}')


def _call_tool(tool, **kwargs):
    for attr in ("_tool_func", "func", "__wrapped__", "original", "_func"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            return fn(**kwargs)
    return tool(**kwargs)


# --------------------------- topology.analyze_diagram ---------------------------


def test_analyze_high_confidence_when_verify_agrees():
    c = _FakeClient([_GRAPH, '{"faithful":true,"confidence":"high","discrepancies":[]}'])
    a = T.analyze_diagram(b"img", client=c)
    assert a is not None
    assert a.confidence == "high"
    assert a.needs_review is False
    assert a.verified is True
    assert "web" in a.to_prompt_block() and "VERIFIED TOPOLOGY" in a.to_prompt_block()


def test_analyze_low_confidence_flags_and_surfaces_discrepancies():
    c = _FakeClient([_GRAPH,
                     '{"faithful":false,"confidence":"low","discrepancies":["missed DR link","invented LB"]}'])
    a = T.analyze_diagram(b"img", client=c)
    assert a.confidence == "low"
    assert a.needs_review is True
    assert "CAUTION" in a.to_prompt_block()
    assert "missed DR link" in a.to_prompt_block()


def test_analyze_non_diagram_returns_none():
    c = _FakeClient(['{"nodes":[],"edges":[],"ha_attributes":{},"notes":"not a diagram"}'])
    assert T.analyze_diagram(b"img", client=c) is None


def test_analyze_malformed_extract_returns_none():
    c = _FakeClient(["no json at all"])
    assert T.analyze_diagram(b"img", client=c) is None


def test_analyze_verify_failure_is_conservative_medium():
    c = _FakeClient([_GRAPH, "garbage"])
    a = T.analyze_diagram(b"img", client=c)
    assert a.confidence == "medium"
    assert a.needs_review is True


def test_verify_skipped_when_flag_off(monkeypatch):
    monkeypatch.setattr(T, "TOPOLOGY_VERIFY", False)
    c = _FakeClient([_GRAPH])  # only one call — verify must NOT run
    a = T.analyze_diagram(b"img", client=c)
    assert a.confidence == "medium"
    assert c.calls == 1


def test_converse_uses_adaptive_thinking_for_opus(monkeypatch):
    """Opus must be called with adaptive thinking + no temperature."""
    captured = {}
    class Capture:
        def converse(self, **kw):
            captured.update(kw)
            return {"output": {"message": {"content": [{"text": _GRAPH}]}}}
    monkeypatch.setattr(T, "DIAGRAM_MODEL_ID", "us.anthropic.claude-opus-4-8")
    T.extract_topology(b"img", client=Capture())
    assert captured["additionalModelRequestFields"] == {"thinking": {"type": "adaptive"}}
    assert "temperature" not in captured["inferenceConfig"]


# --------------------------- persistence via make_save_standard ---------------------------


class _MemStore:
    def __init__(self): self.saved = []
    def create_standard(self, s): self.saved.append(s)
    def list_standards(self, **kw): return list(self.saved)


def test_low_confidence_topology_marks_rule_needs_review():
    store = _MemStore()
    ctx = ExtractionContext(document_id="d", filename="sdd.pptx", hosting_scope="both",
                            val_store=store, batch_id="ext-1")
    ctx.topology_by_unit["slide 31"] = {
        "confidence": "low",
        "graph": {"nodes": [{"id": "web"}], "edges": []},
    }
    save = make_save_standard(ctx, unit_label="slide 31")
    # Give it a source_quote so ONLY topology confidence can trip needs_review.
    _call_tool(save, name="Tier1 web AA", description="d", severity="high",
               category="failover", validation_criteria="v", applies_to_tiers=[1],
               layer="web", source_quote="diagram shows A-A", hosting_scope="both")
    s = store.saved[0]
    assert s["topology_confidence"] == "low"
    assert s["topology_graph"]["nodes"][0]["id"] == "web"
    assert s["needs_review"] is True


def test_high_confidence_topology_does_not_force_review():
    store = _MemStore()
    ctx = ExtractionContext(document_id="d", filename="sdd.pptx", hosting_scope="both",
                            val_store=store, batch_id="ext-1")
    ctx.topology_by_unit["slide 12"] = {"confidence": "high", "graph": {"nodes": [], "edges": []}}
    save = make_save_standard(ctx, unit_label="slide 12")
    _call_tool(save, name="R", description="d", severity="high", category="failover",
               validation_criteria="v", applies_to_tiers=[1], layer="web",
               source_quote="verbatim", hosting_scope="both")
    s = store.saved[0]
    assert s["topology_confidence"] == "high"
    assert "needs_review" not in s  # high-confidence + has quote → clean


def test_text_unit_carries_no_topology_metadata():
    store = _MemStore()
    ctx = ExtractionContext(document_id="d", filename="rb.pdf", hosting_scope="both",
                            val_store=store, batch_id="ext-1")
    # No topology_by_unit entry for this unit (text/table unit).
    save = make_save_standard(ctx, unit_label="page 4")
    _call_tool(save, name="R", description="d", severity="high", category="failover",
               validation_criteria="v", applies_to_tiers=[1], layer="web",
               source_quote="verbatim", hosting_scope="both")
    s = store.saved[0]
    assert "topology_confidence" not in s
    assert "topology_graph" not in s
