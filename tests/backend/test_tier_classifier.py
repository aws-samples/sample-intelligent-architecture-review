# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tier classification and CloudWatch metric emission.

The tier decides which rules a document is validated against, so a wrong tier
changes the verdict. Every failure path here must return None ("unknown") rather
than guess a tier — the caller then leaves declared_tier unset and the validator
runs all rules, which is the safe direction.

metrics.py was at 0%: it is fire-and-forget telemetry, and its contract is that
it NEVER raises into the caller. A metric emitter that throws would abort a
validation that had already succeeded.
"""

from __future__ import annotations

import json

import pytest

from app.models.document import Document, DocumentStatus, DocumentType, ParsedSection
from app.services import tier_classifier as tc


def _doc(sections=None, document_id="doc-1") -> Document:
    return Document(
        document_id=document_id,
        filename="sdd.pdf",
        s3_key="k",
        document_type=DocumentType.SDD,
        status=DocumentStatus.PARSED,
        hosting_scope="both",
        sections=list(sections or []),
    )


def _section(page: int, title: str, content: str) -> ParsedSection:
    return ParsedSection(
        section_id=f"s{page}", title=title, content=content, page_number=page
    )


class TestBuildDocumentText:
    def test_joins_sections_with_page_markers(self):
        text = tc._build_document_text(
            _doc([_section(1, "Intro", "body one"), _section(2, "Scope", "body two")])
        )
        assert "[Page 1] Intro" in text
        assert "[Page 2] Scope" in text
        assert "body two" in text

    def test_no_sections_yields_empty_string(self):
        assert tc._build_document_text(_doc([])) == ""

    def test_truncates_at_the_cap(self):
        """Uncapped input would blow the model's context and cost on a 1000-page
        SDD; the marker tells a reader the text was cut."""
        big = _doc([_section(1, "Big", "x" * (tc.MAX_INPUT_CHARS + 5000))])
        text = tc._build_document_text(big)
        assert len(text) < tc.MAX_INPUT_CHARS + 200
        assert "truncated for tier classification" in text

    def test_tolerates_a_document_without_sections_attribute(self):
        class _Bare:
            document_id = "x"

        assert tc._build_document_text(_Bare()) == ""


class TestExtractJson:
    def test_parses_bare_json(self):
        assert tc._extract_json('{"tier": 2}') == {"tier": 2}

    def test_parses_json_inside_a_fence(self):
        assert tc._extract_json('```json\n{"tier": 3}\n```') == {"tier": 3}

    def test_parses_an_unlabelled_fence(self):
        assert tc._extract_json('```\n{"tier": 4}\n```') == {"tier": 4}

    def test_ignores_prose_around_the_object(self):
        """Models add preamble despite being told not to."""
        raw = 'Sure! Here is the result:\n{"tier": 1, "confidence": "high"}\nHope that helps.'
        assert tc._extract_json(raw)["tier"] == 1

    @pytest.mark.parametrize("raw", ["", None, "no json here", "{unclosed", "}{"])
    def test_unparseable_input_returns_none(self, raw):
        assert tc._extract_json(raw) is None

    def test_malformed_json_returns_none_rather_than_raising(self):
        assert tc._extract_json('{"tier": }') is None


class _Converse:
    """Stub bedrock-runtime client returning a scripted response."""

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def converse(self, **kw):
        self.calls.append(kw)
        if isinstance(self.result, Exception):
            raise self.result
        return {"output": {"message": {"content": [{"text": self.result}]}}}


@pytest.fixture
def bedrock(monkeypatch):
    """Replace boto3.client with a scripted converse stub."""
    box: dict = {}

    def _set(result):
        client = _Converse(result)
        box["client"] = client
        monkeypatch.setattr(tc.boto3, "client", lambda *a, **kw: client)
        return client

    return _set


class TestClassifyTier:
    @pytest.mark.parametrize("tier", [1, 2, 3, 4, 5])
    def test_returns_each_valid_tier(self, bedrock, tier):
        bedrock(json.dumps({"tier": tier, "confidence": "high", "rationale": "r"}))
        assert tc.classify_tier(_doc([_section(1, "T", "content")])) == tier

    def test_accepts_a_numeric_string_tier(self, bedrock):
        bedrock(json.dumps({"tier": "2"}))
        assert tc.classify_tier(_doc([_section(1, "T", "c")])) == 2

    def test_skips_the_model_call_when_there_is_no_text(self, bedrock):
        """No parsed text means nothing to classify — and calling Bedrock with
        an empty prompt would spend tokens to get a hallucinated tier."""
        client = bedrock(json.dumps({"tier": 1}))
        assert tc.classify_tier(_doc([])) is None
        assert client.calls == []

    @pytest.mark.parametrize("tier", [0, 6, -1, 99])
    def test_out_of_range_tier_returns_none(self, bedrock, tier):
        bedrock(json.dumps({"tier": tier}))
        assert tc.classify_tier(_doc([_section(1, "T", "c")])) is None

    @pytest.mark.parametrize("tier", ["critical", None, [], {}])
    def test_non_integer_tier_returns_none(self, bedrock, tier):
        bedrock(json.dumps({"tier": tier}))
        assert tc.classify_tier(_doc([_section(1, "T", "c")])) is None

    def test_missing_tier_key_returns_none(self, bedrock):
        bedrock(json.dumps({"confidence": "high"}))
        assert tc.classify_tier(_doc([_section(1, "T", "c")])) is None

    def test_unparseable_response_returns_none(self, bedrock):
        bedrock("I'm not going to answer that.")
        assert tc.classify_tier(_doc([_section(1, "T", "c")])) is None

    def test_a_client_error_returns_none(self, bedrock):
        from botocore.exceptions import ClientError

        bedrock(ClientError({"Error": {"Code": "ThrottlingException"}}, "Converse"))
        assert tc.classify_tier(_doc([_section(1, "T", "c")])) is None

    def test_an_unexpected_exception_returns_none(self, bedrock):
        bedrock(RuntimeError("network died"))
        assert tc.classify_tier(_doc([_section(1, "T", "c")])) is None

    def test_the_document_text_reaches_the_prompt(self, bedrock):
        client = bedrock(json.dumps({"tier": 3}))
        tc.classify_tier(_doc([_section(4, "Payments", "must survive AZ loss")]))
        prompt = client.calls[0]["messages"][0]["content"][0]["text"]
        assert "must survive AZ loss" in prompt
        assert "[Page 4] Payments" in prompt

    def test_multi_block_responses_are_concatenated(self, bedrock, monkeypatch):
        """Bedrock may split the answer across content blocks; reading only the
        first would drop half the JSON."""
        class _Split:
            calls: list = []

            def converse(self, **kw):
                return {
                    "output": {
                        "message": {
                            "content": [{"text": '{"tier":'}, {"text": " 5}"}]
                        }
                    }
                }

        monkeypatch.setattr(tc.boto3, "client", lambda *a, **kw: _Split())
        assert tc.classify_tier(_doc([_section(1, "T", "c")])) == 5


# ---------------------------------------------------------------------------
# metrics — fire-and-forget, must never raise into the caller
# ---------------------------------------------------------------------------

class _CW:
    def __init__(self, fail=None):
        self.puts: list[dict] = []
        self.fail = fail

    def put_metric_data(self, **kw):
        if self.fail:
            raise self.fail
        self.puts.append(kw)


@pytest.fixture
def cw(monkeypatch):
    from app.services import metrics as m

    def _install(fail=None, client=True):
        svc = m.MetricsService()
        svc._client = _CW(fail) if client else None
        if not client:
            # Force the lazy property to keep returning None.
            monkeypatch.setattr(
                m.boto3, "client", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no creds"))
            )
        return svc

    return _install


class TestEmitMetric:
    def test_emits_into_the_project_namespace(self, cw):
        svc = cw()
        svc.emit_metric("ThingHappened", value=3, unit="Count")
        put = svc._client.puts[0]
        assert put["Namespace"] == svc.namespace
        assert svc.namespace.endswith("-metrics")
        assert put["MetricData"][0]["MetricName"] == "ThingHappened"
        assert put["MetricData"][0]["Value"] == 3
        assert put["MetricData"][0]["Unit"] == "Count"

    def test_dimensions_are_converted_to_cloudwatch_shape(self, cw):
        svc = cw()
        svc.emit_metric("M", dimensions={"DocumentId": "doc-9"})
        assert svc._client.puts[0]["MetricData"][0]["Dimensions"] == [
            {"Name": "DocumentId", "Value": "doc-9"}
        ]

    def test_no_dimensions_key_when_none_given(self, cw):
        svc = cw()
        svc.emit_metric("M")
        assert "Dimensions" not in svc._client.puts[0]["MetricData"][0]

    def test_a_client_error_is_swallowed(self, cw):
        """Telemetry must not fail a validation that already succeeded."""
        from botocore.exceptions import ClientError

        svc = cw(fail=ClientError({"Error": {"Code": "AccessDenied"}}, "PutMetricData"))
        svc.emit_metric("M")  # must not raise

    def test_an_unexpected_error_is_swallowed(self, cw):
        svc = cw(fail=RuntimeError("boom"))
        svc.emit_metric("M")  # must not raise

    def test_no_client_means_a_silent_no_op(self, cw):
        svc = cw(client=False)
        svc.emit_metric("M")  # must not raise


class TestValidationCost:
    def test_emits_the_aggregate_and_per_document_series(self, cw):
        svc = cw()
        svc.validation_cost(
            document_id="doc-1",
            validation_id="val-1",
            calls=12,
            input_tokens=1000,
            output_tokens=200,
            estimated_cost_usd=0.42,
            chunks=4,
            duration_seconds=31.5,
        )
        emitted = [p["MetricData"][0] for p in svc._client.puts]
        names = [m["MetricName"] for m in emitted]
        assert names.count("ValidationEstimatedCostUSD") == 2  # aggregate + per-doc
        for expected in (
            "ValidationBedrockCalls",
            "ValidationInputTokens",
            "ValidationOutputTokens",
            "ValidationChunks",
            "ValidationDurationSeconds",
        ):
            assert expected in names

        per_doc = [m for m in emitted if m.get("Dimensions")]
        assert per_doc[0]["Dimensions"] == [{"Name": "DocumentId", "Value": "doc-1"}]

    def test_values_are_passed_through_unmodified(self, cw):
        svc = cw()
        svc.validation_cost(
            document_id="d", validation_id="v", calls=7, input_tokens=11,
            output_tokens=13, estimated_cost_usd=1.25, chunks=3, duration_seconds=9.5,
        )
        by_name = {}
        for p in svc._client.puts:
            m = p["MetricData"][0]
            by_name.setdefault(m["MetricName"], m["Value"])
        assert by_name["ValidationBedrockCalls"] == 7
        assert by_name["ValidationInputTokens"] == 11
        assert by_name["ValidationOutputTokens"] == 13
        assert by_name["ValidationEstimatedCostUSD"] == 1.25
        assert by_name["ValidationChunks"] == 3
        assert by_name["ValidationDurationSeconds"] == 9.5

    def test_duration_uses_the_seconds_unit(self, cw):
        svc = cw()
        svc.validation_cost(
            document_id="d", validation_id="v", calls=1, input_tokens=1,
            output_tokens=1, estimated_cost_usd=0.0, chunks=1, duration_seconds=2.0,
        )
        durations = [
            p["MetricData"][0]
            for p in svc._client.puts
            if p["MetricData"][0]["MetricName"] == "ValidationDurationSeconds"
        ]
        assert durations[0]["Unit"] == "Seconds"

    def test_a_failing_client_does_not_break_the_caller(self, cw):
        svc = cw(fail=RuntimeError("cloudwatch down"))
        svc.validation_cost(
            document_id="d", validation_id="v", calls=1, input_tokens=1,
            output_tokens=1, estimated_cost_usd=0.0, chunks=1, duration_seconds=1.0,
        )  # must not raise
