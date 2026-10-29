# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Extended thinking on the chunked + PPTX validators.

Mirrors the multimodal path. The two paths
that still emit findings without reasoning are:

  - parallel_validator._evaluate_chunk_sync (text-only PDF chunks)
  - parallel_validator.evaluate_slide_batch (PPTX slides)

Both need:
  1. Thinking enabled via the per-path env helper
  2. maxTokens > budget_tokens when thinking on (Bedrock requirement)
  3. reasoning_text + reasoning_signature parsed and stored on the
     intermediate partial
  4. Trace dict on the merged Finding gains reasoning, model_id,
     reasoning_signature

Verified against Bedrock: both `claude-sonnet-4-20250514-v1:0` AND
`claude-sonnet-4-6` support thinking on Bedrock — no model upgrade
needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _bedrock_response_with_reasoning(
    reasoning_text: str,
    answer_text: str,
    signature: str = "sig-abc",
) -> dict:
    """Bedrock Converse response shape, as returned by the service."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "reasoningContent": {
                            "reasoningText": {
                                "text": reasoning_text,
                                "signature": signature,
                            }
                        }
                    },
                    {"text": answer_text},
                ],
            }
        },
        "usage": {"inputTokens": 100, "outputTokens": 200, "totalTokens": 300},
    }


# ---------------------------------------------------------------------------
# Chunked text-only path (Step 3)
# ---------------------------------------------------------------------------


def test_partial_carries_reasoning_text_and_signature():
    """The _Partial dataclass must accept reasoning_text and
    reasoning_signature so _evaluate_chunk_sync can plumb them
    through to _merge_partials."""
    from app.services.parallel_validator import _Partial

    p = _Partial(
        chunk_index=0,
        page_start=1,
        page_end=2,
        status="pass",
        evidence="ev",
        recommendation="",
        affected=[],
        source_section=None,
        reasoning_text="The model thought about it.",
        reasoning_signature="sig-xyz",
    )
    assert p.reasoning_text == "The model thought about it."
    assert p.reasoning_signature == "sig-xyz"


def test_evaluate_chunk_sync_captures_reasoning_when_thinking_on(monkeypatch):
    """When ENABLE_THINKING_CHUNKED=true, _evaluate_chunk_sync must:
      1. Pass additionalModelRequestFields with thinking config
      2. Bump maxTokens above thinking budget (Bedrock validation)
      3. Parse reasoning from the response and store on _Partial
    """
    from app.services.parallel_validator import _evaluate_chunk_sync, _Chunk

    monkeypatch.setenv("ENABLE_THINKING_CHUNKED", "true")

    fake_client = MagicMock()
    fake_client.converse.return_value = _bedrock_response_with_reasoning(
        "Looking at this chunk, the system uses Multi-AZ. So pass.",
        '{"status": "pass", "evidence": "Multi-AZ on page 5", '
        '"recommendation": "", "affected_components": ""}',
        signature="opaque-chunked-sig",
    )

    chunk = _Chunk(
        index=0,
        page_start=1,
        page_end=10,
        text="Some chunk text",
    )
    standard = {
        "id": "HA-001",
        "name": "Multi-AZ",
        "description": "DB must be Multi-AZ",
        "severity": "critical",
        "validation_criteria": "Look for Multi-AZ",
        "applies_to_tiers": [1],
        "hosting_scope": "on_prem",
    }

    partial = _evaluate_chunk_sync(fake_client, chunk, 1, standard)

    # 1. Converse was called with thinking config
    call = fake_client.converse.call_args
    assert "additionalModelRequestFields" in call.kwargs, \
        "ENABLE_THINKING_CHUNKED=true should add thinking to request"
    assert call.kwargs["additionalModelRequestFields"]["thinking"]["type"] == "enabled"
    # 2. maxTokens bumped above budget_tokens
    budget = call.kwargs["additionalModelRequestFields"]["thinking"]["budget_tokens"]
    max_t = call.kwargs["inferenceConfig"]["maxTokens"]
    assert max_t > budget, f"maxTokens ({max_t}) must exceed budget_tokens ({budget})"
    # 3. Reasoning is on the partial
    assert "Multi-AZ" in partial.reasoning_text
    assert partial.reasoning_signature == "opaque-chunked-sig"


def test_evaluate_chunk_sync_skips_thinking_when_disabled(monkeypatch):
    """Default — ENABLE_THINKING_CHUNKED is false. Converse should not
    receive additionalModelRequestFields, and partial.reasoning_text
    should be empty."""
    from app.services.parallel_validator import _evaluate_chunk_sync, _Chunk

    monkeypatch.setenv("ENABLE_THINKING_CHUNKED", "false")

    fake_client = MagicMock()
    fake_client.converse.return_value = {
        "output": {"message": {"content": [
            {"text": '{"status":"pass","evidence":"e","recommendation":"",'
                     '"affected_components":""}'}
        ]}},
        "usage": {"inputTokens": 10, "outputTokens": 20},
    }

    chunk = _Chunk(index=0, page_start=1, page_end=1, text="t")
    standard = {
        "id": "HA-001", "name": "x", "description": "y",
        "severity": "low", "validation_criteria": "z",
        "applies_to_tiers": [1], "hosting_scope": "on_prem",
    }

    partial = _evaluate_chunk_sync(fake_client, chunk, 1, standard)

    call = fake_client.converse.call_args
    assert "additionalModelRequestFields" not in call.kwargs
    assert partial.reasoning_text == ""
    assert partial.reasoning_signature is None


def test_merge_partials_propagates_reasoning_to_trace():
    """The winning partial's reasoning + signature + model_id must
    appear on Finding.trace for the chat panel and trace drawer."""
    from app.models.validation import ComplianceStatus
    from app.services.parallel_validator import _merge_partials, _Partial

    p_pass = _Partial(
        chunk_index=0,
        page_start=1,
        page_end=5,
        status="pass",
        evidence="page 3 shows Multi-AZ",
        recommendation="",
        affected=["RDS"],
        source_section="Architecture",
        prompt="the prompt",
        raw_response='{"status":"pass"}',
        chunk_excerpt="chunk text",
        reasoning_text="I considered the chunk and decided pass.",
        reasoning_signature="sig-real",
        input_tokens=100,
        output_tokens=50,
        latency_ms=500,
    )

    finding = _merge_partials(
        {"id": "HA-001", "name": "Multi-AZ", "severity": "critical"},
        [p_pass],
    )

    assert finding.status == ComplianceStatus.PASS
    assert finding.trace is not None
    assert finding.trace.get("reasoning") == "I considered the chunk and decided pass."
    assert finding.trace.get("reasoning_signature") == "sig-real"
    assert finding.trace.get("model_id"), "model_id must be on trace"


def test_merge_absent_trace_includes_reasoning():
    """Critic Critical-3: when no chunk addressed the rule, the
    synthesized FAIL must still carry the model's "I didn't find this"
    reasoning so users can audit the search."""
    from app.services.parallel_validator import _merge_partials, _Partial

    p_unaddressed = _Partial(
        chunk_index=0,
        page_start=1,
        page_end=5,
        status="not_addressed",
        evidence="",
        recommendation="",
        affected=[],
        source_section=None,
        prompt="the prompt",
        raw_response='{"status":"not_addressed"}',
        chunk_excerpt="chunk text",
        reasoning_text="I scanned the chunk and saw no mention of Multi-AZ.",
        reasoning_signature="sig-abs",
    )

    finding = _merge_partials(
        {"id": "HA-001", "name": "Multi-AZ", "severity": "critical"},
        [p_unaddressed],
    )

    assert finding.trace is not None
    assert "no mention" in finding.trace.get("reasoning", "")
    assert finding.trace.get("reasoning_signature") == "sig-abs"


# ---------------------------------------------------------------------------
# PPTX path (Step 4)
# ---------------------------------------------------------------------------


def test_pptx_path_helpers_exist():
    """Sanity — the per-path env helpers must exist so the PPTX
    evaluator can gate thinking on/off the same way multimodal does."""
    from app.services.pdf_multimodal import (
        _thinking_enabled_pptx,
        _thinking_enabled_chunked,
        _thinking_enabled_multimodal,
    )

    # Defaults — multimodal + pptx ON, chunked OFF (cost cliff).
    assert _thinking_enabled_multimodal() is True
    assert _thinking_enabled_pptx() is True
    assert _thinking_enabled_chunked() is False


# NOTE: the _should_use_agent seam and its 5 tests were removed
# with the AgentCore decommission . That predicate gated a
# never-built agentic validation path; validation is deterministic
# (parallel fan-out + decision tree), so the seam contradicted the
# actual architecture and had zero callers.
