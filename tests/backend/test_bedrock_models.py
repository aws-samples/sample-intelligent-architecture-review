# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Model IDs come from one place.

A model version written down in a dozen files is a migration hazard: one gets
missed and two pipeline stages silently disagree. These tests keep
app/bedrock_models.py the only place a literal appears, and check the override
mechanism behaves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import bedrock_models

BACKEND = Path(__file__).resolve().parents[2] / "backend"
MODEL_LITERAL = re.compile(r'"(?:us|global|eu|apac)\.anthropic\.claude[^"]*"')

# Where a literal model id is legitimately allowed to appear.
ALLOWED = {
    Path("app/bedrock_models.py"),          # the source of truth itself
}


def _python_sources():
    for path in (BACKEND / "app").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_model_ids_are_declared_in_exactly_one_module():
    offenders = []
    for path in _python_sources():
        rel = path.relative_to(BACKEND)
        if rel in ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            # Ignore prose in comments/docstrings that merely names a model.
            stripped = line.lstrip()
            if stripped.startswith(("#", "*")):
                continue
            for match in MODEL_LITERAL.finditer(line):
                offenders.append(f"{rel}:{lineno}: {match.group(0)}")
    assert not offenders, (
        "hardcoded Bedrock model id(s) outside app/bedrock_models.py:\n"
        + "\n".join(offenders)
        + "\n\nImport a tier constant (SONNET / OPUS_REASONING / OPUS_VISION) "
          "and resolve overrides with model_for(env_var, default)."
    )


def test_tier_constants_are_inference_profiles():
    """Bedrock IAM is scoped to inference-profile/us.anthropic.*; a bare model id
    or a different region prefix would be denied at call time."""
    for name in ("SONNET", "OPUS_REASONING", "OPUS_VISION"):
        value = getattr(bedrock_models, name)
        assert value.startswith("us.anthropic.claude"), f"{name} = {value!r}"


def test_tiers_are_distinct():
    tiers = {bedrock_models.SONNET,
             bedrock_models.OPUS_REASONING,
             bedrock_models.OPUS_VISION}
    assert len(tiers) == 3, "the three tiers should be three different models"


class TestModelFor:
    def test_falls_back_to_the_tier_default(self, monkeypatch):
        monkeypatch.delenv("SOME_STAGE_MODEL_ID", raising=False)
        assert bedrock_models.model_for(
            "SOME_STAGE_MODEL_ID", bedrock_models.SONNET
        ) == bedrock_models.SONNET

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("SOME_STAGE_MODEL_ID", "us.anthropic.claude-opus-4-9")
        assert bedrock_models.model_for(
            "SOME_STAGE_MODEL_ID", bedrock_models.SONNET
        ) == "us.anthropic.claude-opus-4-9"

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_env_var_falls_back(self, monkeypatch, blank):
        """A var that is set-but-empty is a common deploy slip; it must not
        produce an empty modelId that fails at the Bedrock call."""
        monkeypatch.setenv("SOME_STAGE_MODEL_ID", blank)
        assert bedrock_models.model_for(
            "SOME_STAGE_MODEL_ID", bedrock_models.OPUS_VISION
        ) == bedrock_models.OPUS_VISION

    def test_surrounding_whitespace_is_trimmed(self, monkeypatch):
        monkeypatch.setenv("SOME_STAGE_MODEL_ID", "  us.anthropic.claude-opus-4-8  ")
        assert bedrock_models.model_for(
            "SOME_STAGE_MODEL_ID", bedrock_models.SONNET
        ) == "us.anthropic.claude-opus-4-8"


class TestIsOpus:
    """The thinking-request shape is chosen from this, so a wrong answer means a
    Bedrock ValidationException at call time."""

    @pytest.mark.parametrize("model_id", [
        "us.anthropic.claude-opus-4-8",
        "us.anthropic.claude-opus-4-7",
        "global.anthropic.claude-opus-4-8",
        "eu.anthropic.claude-opus-4-8",
        "apac.anthropic.claude-opus-4-8",
    ])
    def test_recognises_opus_across_region_profiles(self, model_id):
        assert bedrock_models.is_opus(model_id)

    @pytest.mark.parametrize("model_id", [
        "us.anthropic.claude-sonnet-4-6",
        "global.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-haiku-4-5",
        "",
    ])
    def test_rejects_non_opus(self, model_id):
        assert not bedrock_models.is_opus(model_id)

    def test_the_vision_tier_is_opus(self):
        assert bedrock_models.is_opus(bedrock_models.OPUS_VISION)
        assert bedrock_models.is_opus(bedrock_models.OPUS_REASONING)
        assert not bedrock_models.is_opus(bedrock_models.SONNET)


def test_every_stage_resolves_to_a_real_model():
    """Import each model-using module and assert its resolved id is non-empty and
    well-formed, so a bad refactor can't leave a stage pointing at nothing."""
    from app.agents.extraction import agent, multimodal, synthesis, topology
    from app.services import (
        parallel_validator, rules_curator_chat, standards_curator, tier_classifier,
    )

    resolved = {
        "validation": parallel_validator.MODEL_ID,
        "curator_chat": rules_curator_chat.MODEL_ID,
        "curator": standards_curator.MODEL_ID,
        "tier_classifier": tier_classifier.MODEL_ID,
        "extraction_unit": multimodal.UNIT_MODEL_ID,
        "extraction_synth": synthesis.SYNTH_MODEL_ID,
        "diagram": topology.DIAGRAM_MODEL_ID,
        "diagram_unit": agent.DIAGRAM_UNIT_MODEL_ID,
    }
    for stage, model_id in resolved.items():
        assert model_id.startswith("us.anthropic.claude"), f"{stage} -> {model_id!r}"

    # Diagram work must stay on the vision tier — topology reads pixels, and the
    # two diagram entry points must agree or extraction and its verification pass
    # would read the same image with different models.
    assert resolved["diagram"] == resolved["diagram_unit"]
