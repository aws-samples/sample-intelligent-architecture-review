# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Hosting-scope inference and the mismatch verdict.

hosting_scope is declared at upload and forced onto every extracted rule, so a
wrong selection silently mislabels the whole batch. This classifier lets the
review gate WARN on that — but only when it is confident, and never by
overriding the human. Every failure path returns None ("no opinion"), which the
caller must not read as a mismatch, and the mismatch logic must not fire on the
cases that would make the warning noisy (a "both" declaration, a low-confidence
guess, an inferred "both").
"""

from __future__ import annotations

import json

import pytest

from app.services import scope_classifier as sc


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
    box: dict = {}

    def _set(result):
        client = _Converse(result)
        box["client"] = client
        # scope_classifier builds its client through _client(); patch boto3 the
        # same way tier_classifier tests do.
        monkeypatch.setattr(sc.boto3, "client", lambda *a, **kw: client)
        return client

    return _set


def _reply(scope, confidence="high", rationale="r"):
    return json.dumps({"scope": scope, "confidence": confidence, "rationale": rationale})


class TestClassifyScope:
    @pytest.mark.parametrize("scope", ["on_prem", "cloud", "both"])
    def test_returns_each_valid_scope(self, bedrock, scope):
        bedrock(_reply(scope))
        assert sc.classify_scope("some doc")["scope"] == scope

    def test_normalizes_a_hyphenated_scope(self, bedrock):
        bedrock(_reply("on-prem"))
        assert sc.classify_scope("d")["scope"] == "on_prem"

    def test_empty_text_skips_the_model(self, bedrock):
        client = bedrock(_reply("cloud"))
        assert sc.classify_scope("   ") is None
        assert client.calls == []

    def test_out_of_enum_scope_returns_none(self, bedrock):
        bedrock(_reply("hybrid-ish"))
        assert sc.classify_scope("d") is None

    def test_unparseable_response_returns_none(self, bedrock):
        bedrock("not json at all")
        assert sc.classify_scope("d") is None

    def test_bad_confidence_is_coerced_to_low(self, bedrock):
        bedrock(_reply("cloud", confidence="pretty sure"))
        assert sc.classify_scope("d")["confidence"] == "low"

    def test_a_client_error_returns_none(self, bedrock):
        from botocore.exceptions import ClientError
        bedrock(ClientError({"Error": {"Code": "AccessDenied"}}, "Converse"))
        assert sc.classify_scope("d") is None

    def test_an_unexpected_exception_returns_none(self, bedrock):
        bedrock(RuntimeError("boom"))
        assert sc.classify_scope("d") is None

    def test_the_document_text_reaches_the_prompt(self, bedrock):
        client = bedrock(_reply("cloud"))
        sc.classify_scope("MARKER-DATACENTER-TEXT")
        assert "MARKER-DATACENTER-TEXT" in client.calls[0]["messages"][0]["content"][0]["text"]


class TestDetectMismatch:
    def test_flags_a_confident_contradiction(self, bedrock):
        bedrock(_reply("cloud", confidence="high"))
        m = sc.detect_scope_mismatch("on_prem", "AWS regions, managed RDS")
        assert m and m["declared"] == "on_prem" and m["inferred"] == "cloud"

    def test_agreement_is_not_a_mismatch(self, bedrock):
        bedrock(_reply("cloud", confidence="high"))
        assert sc.detect_scope_mismatch("cloud", "d") is None

    def test_a_declared_both_is_never_a_mismatch(self, bedrock):
        # "both" accepts anything, so it can't be contradicted — and calling the
        # model would just waste a request.
        client = bedrock(_reply("cloud", confidence="high"))
        assert sc.detect_scope_mismatch("both", "d") is None
        assert client.calls == []

    def test_low_confidence_does_not_second_guess_the_human(self, bedrock):
        bedrock(_reply("cloud", confidence="low"))
        assert sc.detect_scope_mismatch("on_prem", "d") is None

    def test_inferred_both_does_not_flag_a_concrete_declaration(self, bedrock):
        # "this looks hybrid" is weaker than "definitely cloud" and would warn on
        # the common doc that mentions both worlds in passing.
        bedrock(_reply("both", confidence="high"))
        assert sc.detect_scope_mismatch("on_prem", "d") is None

    def test_a_model_failure_is_no_opinion_not_a_mismatch(self, bedrock):
        bedrock(RuntimeError("down"))
        assert sc.detect_scope_mismatch("on_prem", "d") is None
