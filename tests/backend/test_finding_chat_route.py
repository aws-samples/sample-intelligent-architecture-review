# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""chat panel grounded in real reasoning + verdict-flip detector.

The chat endpoint already loads rule + verdict + trace context. v3 adds:
1. The system prompt explicitly leads with the captured reasoning and
   forbids the model from inventing reasoning beyond what's there.
2. An output-side regex detects verdict-flip / counterfactual hedging
   in the model's reply and appends a ⚠ disclaimer block. This is the
   audit-grade belt-and-suspenders the system prompt alone can't provide.
"""

from __future__ import annotations

import re

import pytest


# ---------------------------------------------------------------------------
# Verdict-flip detector — pure-function tests, no Bedrock involved
# ---------------------------------------------------------------------------


def test_verdict_flip_detector_flags_direct_should_have_passed():
    """The "verdict should have been different" pattern."""
    from app.api.routes.validation import _attach_verdict_flip_warning

    out = _attach_verdict_flip_warning(
        "Looking at the evidence, this should have passed.", "fail"
    )
    assert "⚠" in out
    assert "stored verdict is **fail**" in out


def test_verdict_flip_detector_flags_actually_is_pass():
    from app.api.routes.validation import _attach_verdict_flip_warning

    out = _attach_verdict_flip_warning(
        "Given the configuration, this actually is a pass.", "fail"
    )
    assert "⚠" in out


def test_verdict_flip_detector_flags_counterfactual_hedging():
    """Critic N2 — auditor's nightmare: 'if X were enabled, this would
    pass'. v4 adds a counterfactual pattern."""
    from app.api.routes.validation import _attach_verdict_flip_warning

    out = _attach_verdict_flip_warning(
        "If Multi-AZ were enabled, this rule would be satisfied.", "fail"
    )
    assert "⚠" in out


def test_verdict_flip_detector_flags_overlooked_evidence():
    """Insinuation that the validator missed evidence."""
    from app.api.routes.validation import _attach_verdict_flip_warning

    out = _attach_verdict_flip_warning(
        "The validator may have overlooked the redundancy diagram.", "fail"
    )
    assert "⚠" in out


def test_verdict_flip_detector_does_not_flag_rule_quoting():
    """Critic v3 #2 — false-positive on educational rule-quoting like
    'the rule says it passes if Multi-AZ is enabled'. The lookbehind
    must skip these."""
    from app.api.routes.validation import _attach_verdict_flip_warning

    out = _attach_verdict_flip_warning(
        "The rule says the system passes if Multi-AZ is enabled. "
        "Multi-AZ is mentioned on page 12, so this passes.",
        "pass",
    )
    # Should NOT have a warning — this is rule-quoting, not flipping.
    assert "⚠" not in out


def test_verdict_flip_detector_does_not_flag_neutral_explanation():
    """Plain explanations of how the validator reached the verdict
    should never trigger."""
    from app.api.routes.validation import _attach_verdict_flip_warning

    out = _attach_verdict_flip_warning(
        "The validator found explicit evidence on page 6 that the "
        "system uses Aurora Global Database, satisfying the rule.",
        "pass",
    )
    assert "⚠" not in out


def test_verdict_flip_detector_preserves_original_when_no_flip():
    """Replies that don't trigger should round-trip unchanged."""
    from app.api.routes.validation import _attach_verdict_flip_warning

    msg = "Looking at the trace, the model cited page 6 specifically."
    out = _attach_verdict_flip_warning(msg, "pass")
    assert out == msg


# ---------------------------------------------------------------------------
# Chat system prompt — verifies the reasoning block is included and
# the guardrail language is present.
# ---------------------------------------------------------------------------


def _make_finding_with_reasoning(reasoning_text: str = "I looked at page 6 and saw Aurora."):
    """Construct a Finding with the trace shape."""
    from app.models.validation import (
        ComplianceStatus,
        Finding,
        Severity,
    )

    return Finding(
        finding_id="find-test-001",
        requirement_id="HA-D-X",
        requirement_title="Test rule",
        severity=Severity.CRITICAL,
        status=ComplianceStatus.PASS,
        evidence="Page 6 says Aurora.",
        recommendation="",
        affected_components=[],
        source_section=None,
        trace={
            "prompt": "the prompt sent",
            "chunk_excerpt": "[Page 6] Aurora is configured...",
            "raw_response": '[{"verdict":"pass"...}]',
            "reasoning": reasoning_text,
            "reasoning_signature": "opaque-sig",
            "model_id": "us.anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 100,
            "output_tokens": 50,
            "latency_ms": 500,
        },
    )


def test_chat_system_prompt_includes_reasoning_block():
    """The system prompt builder must include the captured reasoning,
    distinct from the raw_response."""
    from app.api.routes.validation import _build_chat_system_prompt

    finding = _make_finding_with_reasoning("Specific reasoning text from the model.")
    prompt = _build_chat_system_prompt(
        finding=finding,
        rule={"id": "HA-D-X", "name": "Test rule"},
        document=None,
        validation=None,
    )

    assert "Specific reasoning text from the model." in prompt
    assert "verbalized reasoning" in prompt.lower()


def test_chat_system_prompt_forbids_verdict_change():
    """The prompt must explicitly tell the model it cannot change the verdict."""
    from app.api.routes.validation import _build_chat_system_prompt

    finding = _make_finding_with_reasoning()
    prompt = _build_chat_system_prompt(
        finding=finding,
        rule={"id": "HA-D-X", "name": "Test rule"},
        document=None,
        validation=None,
    )
    assert "may NOT change the verdict" in prompt or "cannot change the verdict" in prompt.lower()


def test_chat_system_prompt_handles_missing_reasoning():
    """When trace.reasoning is None, the prompt must say so explicitly
    and tell the model to decline reasoning-based answers."""
    from app.api.routes.validation import _build_chat_system_prompt
    from app.models.validation import (
        ComplianceStatus,
        Finding,
        Severity,
    )

    finding = Finding(
        finding_id="find-no-reasoning",
        requirement_id="HA-D-Y",
        requirement_title="Old rule",
        severity=Severity.HIGH,
        status=ComplianceStatus.FAIL,
        evidence="...",
        recommendation="",
        affected_components=[],
        source_section=None,
        trace=None,
    )
    prompt = _build_chat_system_prompt(
        finding=finding,
        rule={"id": "HA-D-Y", "name": "Old rule"},
        document=None,
        validation=None,
    )
    # Must explicitly note absence
    assert "not captured" in prompt.lower() or "not available" in prompt.lower()
