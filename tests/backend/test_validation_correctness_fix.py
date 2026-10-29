# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Validation-correctness — regression tests for the 0/100 collapse.

Covers the confirmed root causes:
- vacuous 100 when no rules apply -> now None (not-applicable)
- "not addressed" forced to FAIL -> now NOT_ADDRESSED, excluded from score
- PASS_VIA_SUBSTITUTION earning zero -> now full credit
- N/A-via-exception dragging the denominator -> now excluded
- decision tree skipped on multimodal/PPTX -> resolve_decision_tree shared
"""

from __future__ import annotations

import uuid

from app.models.validation import (
    ComplianceStatus,
    Finding,
    LayerVerdict,
    Severity,
)
from app.services.parallel_validator import (
    _calculate_score,
    resolve_decision_tree,
)


def _f(status, severity=Severity.HIGH, rid=None, rule_type="founding"):
    return Finding(
        finding_id=f"find-{uuid.uuid4().hex[:8]}",
        requirement_id=rid or f"R-{uuid.uuid4().hex[:6]}",
        requirement_title="t",
        severity=severity,
        status=status,
        evidence="e",
        rule_type=rule_type,
    )


# ---- _calculate_score ----

def test_empty_findings_scores_none_not_100():
    assert _calculate_score([]) is None


def test_all_not_addressed_scores_none():
    # A doc silent on every rule is not 0% and not 100% — it's N/A.
    findings = [_f(ComplianceStatus.NOT_ADDRESSED) for _ in range(3)]
    assert _calculate_score(findings) is None


def test_all_not_applicable_scores_none():
    findings = [
        _f(ComplianceStatus.NOT_APPLICABLE),
        _f(ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION),
    ]
    assert _calculate_score(findings) is None


def test_not_addressed_excluded_from_denominator():
    # One PASS + one NOT_ADDRESSED -> 100 (the not-addressed doesn't count),
    # NOT 50. This is the core fix: sparse coverage no longer reads as failure.
    findings = [
        _f(ComplianceStatus.PASS, Severity.HIGH),
        _f(ComplianceStatus.NOT_ADDRESSED, Severity.HIGH),
    ]
    assert _calculate_score(findings) == 100.0


def test_pass_via_substitution_earns_full_credit():
    findings = [
        _f(ComplianceStatus.PASS_VIA_SUBSTITUTION, Severity.HIGH),
        _f(ComplianceStatus.FAIL, Severity.HIGH),
    ]
    # substitution counts as pass -> 1 of 2 weighted -> 50, not 0.
    assert _calculate_score(findings) == 50.0


def test_na_via_exception_not_in_denominator():
    # PASS + N/A-via-exception -> 100 (exempted rule excluded), not 50.
    findings = [
        _f(ComplianceStatus.PASS, Severity.HIGH),
        _f(ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION, Severity.HIGH),
    ]
    assert _calculate_score(findings) == 100.0


def test_warning_half_credit():
    findings = [_f(ComplianceStatus.WARNING, Severity.MEDIUM)]
    assert _calculate_score(findings) == 50.0


def test_fail_scores_zero_but_that_is_real():
    # A genuine FAIL still scores 0 — that's correct, not the bug.
    findings = [_f(ComplianceStatus.FAIL, Severity.HIGH)]
    assert _calculate_score(findings) == 0.0


# ---- resolve_decision_tree (shared across all paths) ----

def test_resolve_decision_tree_exception_marks_founding_na():
    """A passing rule-exception marks its founding rule N/A-via-exception —
    the semantics that were SKIPPED on multimodal/PPTX before this fix."""
    founding = _f(ComplianceStatus.FAIL, rid="F1", rule_type="founding")
    exception = _f(ComplianceStatus.PASS, rid="E1", rule_type="exception")
    standards = [
        {"id": "F1", "rule_type": "founding", "layer": "compute",
         "applies_to_tiers": [1], "hosting_scope": "both"},
        {"id": "E1", "rule_type": "exception", "exception_scope": "rule",
         "parent_rule_id": "F1", "layer": "compute",
         "applies_to_tiers": [1], "hosting_scope": "both"},
    ]
    findings, layer_verdicts, arch = resolve_decision_tree(
        [founding, exception], standards, tier_used=1
    )
    by_id = {f.requirement_id: f for f in findings}
    assert by_id["F1"].status == ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION
    assert by_id["F1"].via_rule_id == "E1"
    # A founding rule exempted -> the score should not be dragged to 0.
    assert _calculate_score(findings) is None or _calculate_score(findings) > 0


def test_resolve_decision_tree_substitution_rescues_fail():
    founding = _f(ComplianceStatus.FAIL, rid="F1", rule_type="founding")
    sub = _f(ComplianceStatus.PASS, rid="S1", rule_type="substitution")
    standards = [
        {"id": "F1", "rule_type": "founding", "layer": "network",
         "applies_to_tiers": [2], "hosting_scope": "both"},
        {"id": "S1", "rule_type": "substitution", "parent_rule_id": "F1",
         "layer": "network", "applies_to_tiers": [2], "hosting_scope": "both"},
    ]
    findings, layer_verdicts, arch = resolve_decision_tree(
        [founding, sub], standards, tier_used=2
    )
    by_id = {f.requirement_id: f for f in findings}
    assert by_id["F1"].status == ComplianceStatus.PASS_VIA_SUBSTITUTION
    assert by_id["F1"].via_rule_id == "S1"


def test_resolve_decision_tree_returns_architecture_verdict():
    founding = _f(ComplianceStatus.PASS, rid="F1", rule_type="founding")
    standards = [
        {"id": "F1", "rule_type": "founding", "layer": "compute",
         "applies_to_tiers": [1], "hosting_scope": "both"},
    ]
    findings, layer_verdicts, arch = resolve_decision_tree(
        [founding], standards, tier_used=1
    )
    assert layer_verdicts, "expected a layer verdict"
    assert arch == LayerVerdict.COMPLIANT


# ---- cancellation registry ----

def test_cancel_registry_roundtrip():
    from app.services.parallel_validator import (
        request_cancel,
        is_cancel_requested,
        clear_cancel,
    )
    vid = "val-cancel-test"
    assert not is_cancel_requested(vid)
    request_cancel(vid)
    assert is_cancel_requested(vid)
    clear_cancel(vid)
    assert not is_cancel_requested(vid)


def test_prompt_shows_source_quote_and_exception_context():
    """Accuracy enrichment: the chunked prompt must surface the verbatim
    source_quote AND explain an exception rule's relationship to its parent
    (previously neither reached the model)."""
    from app.services.parallel_validator import _build_prompt, _Chunk

    parent = {"id": "F1", "validation_criteria": "Must be multi-AZ.",
              "rule_type": "founding"}
    exception = {
        "id": "E1", "name": "Dev-tier exemption", "severity": "low",
        "description": "Non-prod is exempt.",
        "validation_criteria": "Environment is labelled non-production.",
        "rule_type": "exception", "parent_rule_id": "F1",
        "source_quote": "Non-production workloads are exempt from multi-AZ.",
    }
    chunk = _Chunk(index=0, text="doc text", page_start=1, page_end=1)
    prompt = _build_prompt(chunk, 1, exception, references=None,
                           all_by_id={"F1": parent, "E1": exception})
    assert "Source text (verbatim" in prompt
    assert "Non-production workloads are exempt" in prompt
    assert "EXCEPTION rule" in prompt
    # Parent criteria shown for contrast.
    assert "Must be multi-AZ." in prompt


def test_pptx_prompt_handles_quotes_in_criteria():
    """Regression guard for the json.dumps fix: a rule whose criteria contains
    embedded double-quotes must not break the PPTX prompt (the old f-string
    interpolation could produce invalid/broken JSON)."""
    from app.services.parallel_validator import _build_pptx_validation_prompt
    import json as _json
    from app.models.document import SlideType

    rules = [{
        "id": "HA-Q", "name": 'Rule with "quotes"',
        "validation_criteria": 'Must set flag="true" and mode="active"',
        "source_quote": 'The doc says "always on".',
    }]
    prompt = _build_pptx_validation_prompt(1, 3, SlideType.TEXT_HEAVY, None, rules)
    # The rule line is valid JSON (json.dumps escaped the quotes).
    import re
    line = [l for l in prompt.splitlines() if '"id": "HA-Q"' in l]
    assert line, "rule line not found in prompt"
    parsed = _json.loads(line[0].strip())
    assert parsed["id"] == "HA-Q"
    assert 'flag=\"true\"' in parsed["validation_criteria"] or 'flag="true"' in parsed["validation_criteria"]


def test_should_verify_gating():
    """Verify pass fires only for FAIL on configured severities."""
    from app.services import parallel_validator as pv
    # Ensure the module's gate is on for the test regardless of env.
    orig_enabled = pv.VERIFY_FAILS_ENABLED
    orig_sev = pv.VERIFY_FAILS_SEVERITIES
    pv.VERIFY_FAILS_ENABLED = True
    pv.VERIFY_FAILS_SEVERITIES = {"critical", "high"}
    try:
        assert pv._should_verify(_f(ComplianceStatus.FAIL, Severity.CRITICAL))
        assert pv._should_verify(_f(ComplianceStatus.FAIL, Severity.HIGH))
        # Not a FAIL -> skip.
        assert not pv._should_verify(_f(ComplianceStatus.PASS, Severity.CRITICAL))
        # FAIL but low severity -> skip (cost gating).
        assert not pv._should_verify(_f(ComplianceStatus.FAIL, Severity.LOW))
    finally:
        pv.VERIFY_FAILS_ENABLED = orig_enabled
        pv.VERIFY_FAILS_SEVERITIES = orig_sev


def test_verify_fail_sync_parses_refutation_and_usage(monkeypatch):
    """A mocked Bedrock refutation is parsed into (verdict, in_tok, out_tok)."""
    from unittest.mock import MagicMock
    from app.services import parallel_validator as pv

    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"content": [
            {"text": '{"refuted": true, "confidence": "high", "rationale": "doc shows multi-AZ on p.7"}'}
        ]}},
        "usage": {"inputTokens": 120, "outputTokens": 30},
    }
    finding = _f(ComplianceStatus.FAIL, Severity.CRITICAL)
    finding.trace = {"chunk_excerpt": "page 7 shows multi-AZ"}
    verdict, v_in, v_out = pv._verify_fail_sync(
        client, {"id": "HA-1", "name": "x", "validation_criteria": "c"}, finding
    )
    assert verdict["refuted"] is True and verdict["confidence"] == "high"
    # Usage is returned so the caller can bill it (regression guard for the
    # cost-undercount fix).
    assert (v_in, v_out) == (120, 30)


def test_verify_refuted_helper_leniency():
    from app.services.parallel_validator import _verify_refuted
    # bool True + high/medium -> overturn.
    assert _verify_refuted({"refuted": True, "confidence": "high"})
    assert _verify_refuted({"refuted": True, "confidence": "medium"})
    # String "true" (model returned a stringified bool) still counts.
    assert _verify_refuted({"refuted": "true", "confidence": "high"})
    # Low confidence, false, or missing -> keep the FAIL.
    assert not _verify_refuted({"refuted": True, "confidence": "low"})
    assert not _verify_refuted({"refuted": False, "confidence": "high"})
    assert not _verify_refuted(None)


def test_apply_verify_verdict_downgrades_and_records_without_trace():
    """FAIL -> WARNING on refutation; challenge recorded even when trace=None."""
    from app.services.parallel_validator import _apply_verify_verdict
    f = _f(ComplianceStatus.FAIL, Severity.CRITICAL)
    f.trace = None  # the trace=None edge the review flagged
    _apply_verify_verdict(f, {"refuted": True, "confidence": "high", "rationale": "ok"})
    assert f.status == ComplianceStatus.WARNING
    assert isinstance(f.trace, dict) and "verify_challenge" in f.trace
    assert "downgraded to WARNING" in f.evidence


def test_apply_verify_verdict_keeps_fail_when_not_refuted():
    from app.services.parallel_validator import _apply_verify_verdict
    f = _f(ComplianceStatus.FAIL, Severity.CRITICAL)
    _apply_verify_verdict(f, {"refuted": False, "confidence": "high"})
    assert f.status == ComplianceStatus.FAIL  # unchanged


def test_rule_context_block_substitution_branch():
    from app.services.parallel_validator import _rule_context_block
    sub = {"id": "S1", "rule_type": "substitution", "parent_rule_id": "F1"}
    parent = {"id": "F1", "validation_criteria": "Primary: multi-AZ."}
    block = _rule_context_block(sub, {"F1": parent, "S1": sub})
    assert "SUBSTITUTION rule" in block
    assert "Primary: multi-AZ." in block
    # Missing parent -> no crash, no parent line.
    block2 = _rule_context_block(sub, {"S1": sub})
    assert "SUBSTITUTION rule" in block2


def test_prompt_plain_founding_rule_has_no_relationship_block():
    from app.services.parallel_validator import _build_prompt, _Chunk

    founding = {"id": "F1", "name": "x", "severity": "high",
                "validation_criteria": "c", "rule_type": "founding"}
    chunk = _Chunk(index=0, text="t", page_start=1, page_end=1)
    prompt = _build_prompt(chunk, 1, founding)
    assert "Rule relationship" not in prompt  # no noise for plain rules


def test_cancel_registry_ignores_empty_id():
    from app.services.parallel_validator import (
        request_cancel,
        is_cancel_requested,
    )
    request_cancel("")  # no-op, must not register the empty string
    assert not is_cancel_requested("")
    assert not is_cancel_requested(None)


# ---- score None vs 0.0 (the honest-scoring distinction) ----

def test_real_fail_scores_zero_not_none():
    # A genuine FAIL must score 0.0 (real non-compliance), NOT None. None is
    # reserved for "no evaluable rules". Guards against re-conflating them.
    score = _calculate_score([_f(ComplianceStatus.FAIL, Severity.CRITICAL)])
    assert score == 0.0
    assert score is not None


def test_zero_score_is_falsy_but_not_none():
    # Regression guard for the DDB round-trip bug (H2): 0.0 is falsy, so any
    # `if score` check would wrongly treat a real 0% as absent. Assert the
    # value/identity distinction the persistence layer must preserve.
    score = _calculate_score([_f(ComplianceStatus.FAIL)])
    assert score == 0.0
    assert bool(score) is False  # falsy — why `is not None` is required
    assert score is not None


def test_resolve_decision_tree_tier_none_buckets_together():
    # tier_used=None (SDD has no declared tier) must not crash — rules bucket
    # under a sentinel tier and still roll up.
    founding = _f(ComplianceStatus.PASS, rid="F1", rule_type="founding")
    standards = [
        {"id": "F1", "rule_type": "founding", "layer": "compute",
         "applies_to_tiers": [], "hosting_scope": "both"},
    ]
    findings, layer_verdicts, arch = resolve_decision_tree(
        [founding], standards, tier_used=None
    )
    assert layer_verdicts
    assert arch == LayerVerdict.COMPLIANT


def test_verify_pass_does_not_block_the_event_loop(monkeypatch):
    """The verify pass runs on the API's own event loop (multimodal and PPTX
    paths). Each verify is a blocking Bedrock call; run inline, a few hundred
    FAILs froze the loop long enough for the ALB health check to fail and ECS to
    replace the task, killing every in-flight validation. The pass must hand the
    calls to threads and keep the loop free while they run."""
    import asyncio
    import time

    from app.services import parallel_validator as pv

    monkeypatch.setattr(pv, "VERIFY_FAILS_ENABLED", True)
    monkeypatch.setattr(pv, "VERIFY_FAILS_SEVERITIES", {"critical"})

    def slow_verify(client, std, finding):
        time.sleep(0.2)  # a blocking model call
        return {"refuted": False, "confidence": "high"}, 10, 2

    monkeypatch.setattr(pv, "_verify_fail_sync", slow_verify)
    findings = [_f(ComplianceStatus.FAIL, Severity.CRITICAL) for _ in range(6)]

    async def run() -> tuple[tuple[int, int, int], int, float]:
        ticks = 0
        done = asyncio.Event()

        async def heartbeat() -> None:
            # Stands in for the health check: it can only tick while the
            # loop is free.
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(heartbeat())
        start = time.monotonic()
        usage = await pv._verify_findings_async(None, findings, {}, asyncio.Semaphore(6))
        elapsed = time.monotonic() - start
        done.set()
        await beat
        return usage, ticks, elapsed

    (calls, v_in, v_out), ticks, elapsed = asyncio.run(run())

    assert (calls, v_in, v_out) == (6, 60, 12)
    # Concurrent: six 0.2s calls finish in well under the 1.2s a serial loop takes.
    assert elapsed < 0.8, f"verify pass ran serially ({elapsed:.2f}s)"
    # Responsive: the heartbeat kept ticking throughout.
    assert ticks >= 10, f"event loop was blocked (only {ticks} heartbeat ticks)"
