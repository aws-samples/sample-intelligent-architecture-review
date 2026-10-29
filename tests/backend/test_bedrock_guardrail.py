# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Bedrock guardrail must reach every model call.

A Converse call that omits `guardrailConfig` is simply unguarded — Bedrock does
not report an error, and the response looks normal. That makes the failure mode
silent, so these tests pin the two things that prevent it: the client wrapper
injects the config on every call, and a new call site inherits it by
construction rather than by remembering to add an argument.

The wrapper is also a pass-through when no guardrail is configured, because
local development and a deliberately disabled environment run the same code.
"""

from __future__ import annotations

import pytest

from app.services import bedrock_guardrail as bg


@pytest.fixture(autouse=True)
def _clear_guardrail_env(monkeypatch):
    """Guardrail settings are read per call, so each test starts unconfigured."""
    for var in (
        "BEDROCK_GUARDRAIL_ID",
        "BEDROCK_GUARDRAIL_VERSION",
        "BEDROCK_GUARDRAIL_TRACE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc123")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "3")


class _FakeClient:
    """Records the kwargs each Converse call was made with."""

    def __init__(self, response=None):
        self.calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self.response = response if response is not None else {"stopReason": "end_turn"}

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def converse_stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return {"stream": []}

    def list_foundation_models(self, **kwargs):
        return {"modelSummaries": []}


class TestConfiguration:
    def test_disabled_when_no_id_is_set(self):
        assert bg.is_enabled() is False
        assert bg.guardrail_config() == {}
        assert bg.strands_guardrail_kwargs() == {}

    def test_enabled_when_an_id_is_set(self, configured):
        assert bg.is_enabled() is True

    def test_config_carries_id_version_and_trace(self, configured):
        cfg = bg.guardrail_config()["guardrailConfig"]
        assert cfg["guardrailIdentifier"] == "gr-abc123"
        assert cfg["guardrailVersion"] == "3"
        assert cfg["trace"] == "enabled"

    def test_trace_can_be_disabled(self, configured, monkeypatch):
        monkeypatch.setenv("BEDROCK_GUARDRAIL_TRACE", "disabled")
        assert bg.guardrail_config()["guardrailConfig"]["trace"] == "disabled"

    def test_trace_defaults_to_enabled(self, configured):
        """Without the trace an intervention cannot be attributed to a policy;
        it just looks like an unexplained response."""
        assert bg.guardrail_config()["guardrailConfig"]["trace"] == "enabled"

    def test_settings_are_read_per_call_not_cached_at_import(self, monkeypatch):
        assert bg.is_enabled() is False
        monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-late")
        assert bg.is_enabled() is True

    def test_strands_kwargs_use_the_flat_shape(self, configured):
        """Strands takes guardrail_id / guardrail_version, not guardrailConfig.
        Sending the Converse shape there is silently ignored."""
        kw = bg.strands_guardrail_kwargs()
        assert kw["guardrail_id"] == "gr-abc123"
        assert kw["guardrail_version"] == "3"
        assert kw["guardrail_trace"] == "enabled"
        assert "guardrailConfig" not in kw

    def test_whitespace_only_id_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "   ")
        assert bg.is_enabled() is False


class TestWrapping:
    def test_wrap_is_a_passthrough_when_disabled(self):
        """Local development has no guardrail and must not pay for a proxy."""
        client = _FakeClient()
        assert bg.wrap(client) is client

    def test_wrap_returns_a_proxy_when_enabled(self, configured):
        client = _FakeClient()
        assert bg.wrap(client) is not client

    def test_wrapping_twice_does_not_nest(self, configured):
        once = bg.wrap(_FakeClient())
        assert bg.wrap(once) is once

    def test_unrelated_methods_pass_through(self, configured):
        """The wrapper has to be a drop-in replacement for the boto3 client."""
        wrapped = bg.wrap(_FakeClient())
        assert wrapped.list_foundation_models() == {"modelSummaries": []}


class TestConverseInjection:
    def test_guardrail_is_injected_on_every_converse_call(self, configured):
        client = _FakeClient()
        wrapped = bg.wrap(client)
        wrapped.converse(modelId="m", messages=[])
        wrapped.converse(modelId="m", messages=[])
        assert len(client.calls) == 2
        for call in client.calls:
            assert call["guardrailConfig"]["guardrailIdentifier"] == "gr-abc123"

    def test_nothing_is_injected_when_disabled(self):
        client = _FakeClient()
        bg.wrap(client).converse(modelId="m", messages=[])
        assert "guardrailConfig" not in client.calls[0]

    def test_the_original_arguments_are_preserved(self, configured):
        client = _FakeClient()
        bg.wrap(client).converse(
            modelId="m", messages=[{"role": "user"}],
            inferenceConfig={"maxTokens": 400},
        )
        call = client.calls[0]
        assert call["modelId"] == "m"
        assert call["inferenceConfig"] == {"maxTokens": 400}

    def test_an_explicit_config_from_the_caller_is_not_overwritten(self, configured):
        """A call site that needs a different policy can still say so."""
        client = _FakeClient()
        explicit = {"guardrailIdentifier": "gr-other", "guardrailVersion": "1"}
        bg.wrap(client).converse(modelId="m", guardrailConfig=explicit)
        assert client.calls[0]["guardrailConfig"] is explicit

    def test_streaming_calls_are_also_guarded(self, configured):
        client = _FakeClient()
        bg.wrap(client).converse_stream(modelId="m", messages=[])
        assert client.stream_calls[0]["guardrailConfig"]["guardrailIdentifier"] == (
            "gr-abc123"
        )

    def test_the_response_is_returned_unchanged(self, configured):
        client = _FakeClient(response={"stopReason": "end_turn", "output": {"x": 1}})
        out = bg.wrap(client).converse(modelId="m")
        assert out == {"stopReason": "end_turn", "output": {"x": 1}}


class TestInterventionDetection:
    def test_a_clean_response_reports_nothing(self):
        assert bg.describe_intervention({"stopReason": "end_turn"}) is None

    def test_an_intervention_with_no_assessment_reads_as_blocked(self):
        """With the trace disabled there is no assessment to inspect, so the safe
        reading of guardrail_intervened is that nothing usable came back."""
        got = bg.describe_intervention({"stopReason": "guardrail_intervened"})
        assert got is not None
        assert got["blocked"] is True
        assert got["action"] == "BLOCKED"

    def test_a_redaction_is_not_reported_as_blocked(self):
        """Verified against a live guardrail: Bedrock sets
        stopReason=guardrail_intervened for an ANONYMIZED entity too, and the
        response text is intact with the entity replaced. Treating that as a
        block would fire the blocked-call alarm on every redaction."""
        response = {
            "stopReason": "guardrail_intervened",
            "trace": {"guardrail": {"outputAssessments": {"g": [
                {"sensitiveInformationPolicy": {"piiEntities": [
                    {"match": "a@b.example", "type": "EMAIL", "action": "ANONYMIZED"}
                ]}}
            ]}}},
        }
        got = bg.describe_intervention(response)
        assert got["blocked"] is False
        assert got["action"] == "REDACTED"
        assert "ANONYMIZED" in got["actions"]

    def test_an_explicit_blocked_action_is_reported_as_blocked(self):
        response = {
            "stopReason": "guardrail_intervened",
            "trace": {"guardrail": {"inputAssessment": {"g": {
                "contentPolicy": {"filters": [
                    {"type": "PROMPT_ATTACK", "action": "BLOCKED"}
                ]}
            }}}},
        }
        got = bg.describe_intervention(response)
        assert got["blocked"] is True
        assert got["action"] == "BLOCKED"

    def test_a_redaction_is_detected_without_a_stop_reason(self):
        """A redaction is not a stop reason. Reading only stopReason would miss
        every PII redaction, which is the policy that fires most often here."""
        response = {
            "stopReason": "end_turn",
            "trace": {
                "guardrail": {
                    "inputAssessment": {
                        "gr-abc123": {
                            "sensitiveInformationPolicy": {"piiEntities": [{"type": "EMAIL"}]}
                        }
                    }
                }
            },
        }
        got = bg.describe_intervention(response)
        assert got is not None
        assert got["blocked"] is False
        assert got["action"] == "REDACTED"
        assert "sensitive_information" in got["policies"]

    def test_output_assessments_are_read(self):
        response = {
            "stopReason": "end_turn",
            "trace": {
                "guardrail": {
                    "outputAssessments": {
                        "gr-abc123": [
                            {"contentPolicy": {"filters": [{"type": "PROMPT_ATTACK"}]}}
                        ]
                    }
                }
            },
        }
        got = bg.describe_intervention(response)
        assert got is not None
        assert "content" in got["policies"]

    def test_several_policies_are_reported_together(self):
        response = {
            "stopReason": "guardrail_intervened",
            "trace": {
                "guardrail": {
                    "inputAssessment": {
                        "g": {
                            "sensitiveInformationPolicy": {"piiEntities": []},
                            "contentPolicy": {"filters": []},
                        }
                    }
                }
            },
        }
        got = bg.describe_intervention(response)
        assert set(got["policies"]) == {"sensitive_information", "content"}

    @pytest.mark.parametrize("response", [None, "text", 42, [], {}])
    def test_a_malformed_response_does_not_raise(self, response):
        """This runs on the response path of every model call, so a parse
        failure here would break validations that otherwise succeeded."""
        assert bg.describe_intervention(response) is None

    def test_an_empty_guardrail_trace_reports_nothing(self):
        assert bg.describe_intervention(
            {"stopReason": "end_turn", "trace": {"guardrail": {}}}
        ) is None


class TestInterventionAccounting:
    def test_an_intervention_emits_metrics(self, configured, monkeypatch):
        emitted: list[tuple] = []

        class _Metrics:
            def emit_metric(self, name, value=1.0, unit="Count", dimensions=None):
                emitted.append((name, value))

        import app.services.metrics as metrics_mod

        monkeypatch.setattr(metrics_mod, "metrics_service", _Metrics())
        client = _FakeClient(response={
            "stopReason": "guardrail_intervened",
            "trace": {"guardrail": {"inputAssessment": {"g": {
                "contentPolicy": {"filters": [{"type": "PROMPT_ATTACK", "action": "BLOCKED"}]}
            }}}},
        })
        bg.wrap(client).converse(modelId="m")
        names = [n for n, _ in emitted]
        assert "GuardrailInterventions" in names
        assert "GuardrailBlocked" in names

    def test_a_redaction_does_not_count_as_blocked(self, configured, monkeypatch):
        """The two alarms differ: redaction is expected, blocking is not."""
        emitted: list[str] = []

        class _Metrics:
            def emit_metric(self, name, value=1.0, unit="Count", dimensions=None):
                emitted.append(name)

        import app.services.metrics as metrics_mod

        monkeypatch.setattr(metrics_mod, "metrics_service", _Metrics())
        client = _FakeClient(response={
            "stopReason": "guardrail_intervened",
            "trace": {"guardrail": {"outputAssessments": {"g": [
                {"sensitiveInformationPolicy": {"piiEntities": [
                    {"type": "EMAIL", "action": "ANONYMIZED"}
                ]}}
            ]}}},
        })
        bg.wrap(client).converse(modelId="m")
        assert "GuardrailInterventions" in emitted
        assert "GuardrailBlocked" not in emitted

    def test_a_metrics_failure_does_not_break_the_call(self, configured, monkeypatch):
        class _Broken:
            def emit_metric(self, *a, **kw):
                raise RuntimeError("CloudWatch down")

        import app.services.metrics as metrics_mod

        monkeypatch.setattr(metrics_mod, "metrics_service", _Broken())
        client = _FakeClient(response={"stopReason": "guardrail_intervened"})
        # Must return the response rather than propagating the telemetry failure.
        assert bg.wrap(client).converse(modelId="m")["stopReason"] == (
            "guardrail_intervened"
        )

    def test_a_clean_call_emits_nothing(self, configured, monkeypatch):
        emitted: list[str] = []

        class _Metrics:
            def emit_metric(self, name, value=1.0, unit="Count", dimensions=None):
                emitted.append(name)

        import app.services.metrics as metrics_mod

        monkeypatch.setattr(metrics_mod, "metrics_service", _Metrics())
        bg.wrap(_FakeClient()).converse(modelId="m")
        assert emitted == []


class TestEveryCallSiteIsCovered:
    """The wrapper only helps if the factories actually apply it.

    These read the source rather than calling the factories, because
    constructing a real boto3 client requires credentials and a region.
    """

    def _source(self, module_path: str) -> str:
        import pathlib

        return pathlib.Path(module_path).read_text()

    @pytest.mark.parametrize(
        "path",
        [
            "backend/app/services/parallel_validator.py",
            "backend/app/services/rules_curator_chat.py",
            "backend/app/services/standards_curator.py",
            "backend/app/services/tier_classifier.py",
            "backend/app/agents/tools/document_tools.py",
        ],
    )
    def test_each_bedrock_client_factory_is_wrapped(self, path):
        src = self._source(path)
        assert 'boto3.client(' in src, f"{path} no longer builds a client"
        assert "_wrap_guardrail(" in src, (
            f"{path} builds a bedrock-runtime client without wrapping it, so its "
            f"Converse calls are unguarded"
        )

    @pytest.mark.parametrize(
        "path",
        [
            "backend/app/agents/query_agent.py",
            "backend/app/agents/extraction/agent.py",
        ],
    )
    def test_each_strands_model_receives_guardrail_kwargs(self, path):
        src = self._source(path)
        assert "BedrockModel(" in src
        assert "strands_guardrail_kwargs()" in src, (
            f"{path} builds a Strands BedrockModel without guardrail kwargs; the "
            f"client wrapper does not cover this path"
        )

    def test_the_delegating_modules_reuse_the_wrapped_factory(self):
        """These do not build their own client; they must keep delegating to the
        wrapped one rather than constructing a bare client later."""
        for path in (
            "backend/app/services/pdf_multimodal.py",
            "backend/app/services/icon_interpret.py",
            "backend/app/agents/extraction/topology.py",
        ):
            src = self._source(path)
            assert "_make_bedrock_client" in src, path
            assert 'boto3.client(\n        "bedrock-runtime"' not in src, (
                f"{path} started building its own client, which bypasses the "
                f"guardrail wrapper"
            )
