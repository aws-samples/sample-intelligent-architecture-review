# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure helpers inside the parallel validator: chunking, parsing, verify, scoring.

parallel_validator is the biggest module in the app and sat at 48%. The async
fan-out itself is covered elsewhere; what was untested is the pure logic that
decides what the model sees and what its answer means:

  * _build_chunks decides how the document is split. A chunk that loses its
    page markers produces findings the user cannot locate in the source.
  * _partial_from_response decides what an unparseable model reply means. Mapped
    to the wrong status it either invents a compliance verdict or hides one.
  * _verify_refuted decides when an adversarial second opinion overturns a FAIL.
    Too lenient and it manufactures false passes.
  * _calculate_score must return None, never 100, when nothing was evaluable.
"""

from __future__ import annotations

import pytest

from app.models.document import Document, DocumentStatus, DocumentType, ParsedSection
from app.models.validation import ComplianceStatus, Finding, Severity
from app.services import parallel_validator as pv


def _section(page: int, content: str, title=None) -> ParsedSection:
    return ParsedSection(
        section_id=f"s{page}",
        title=title or f"Section {page}",
        content=content,
        page_number=page,
    )


def _doc(sections) -> Document:
    return Document(
        document_id="doc-1",
        filename="sdd.pdf",
        s3_key="k",
        document_type=DocumentType.SDD,
        status=DocumentStatus.PARSED,
        hosting_scope="both",
        sections=sections,
    )


def _finding(status=ComplianceStatus.FAIL, severity=Severity.HIGH, **over) -> Finding:
    base = dict(
        finding_id="f1",
        requirement_id="r1",
        requirement_title="Multi-AZ",
        severity=severity,
        status=status,
        evidence="single AZ only",
    )
    base.update(over)
    return Finding(**base)


class TestBuildChunks:
    def test_a_small_document_is_one_chunk(self, ):
        chunks = pv._build_chunks(_doc([_section(1, "short body")]))
        assert len(chunks) == 1
        assert chunks[0].index == 0
        assert "short body" in chunks[0].text

    def test_page_markers_are_embedded_for_traceability(self):
        """Findings cite pages by scanning these markers; without them a user
        cannot locate the evidence in the source document."""
        chunks = pv._build_chunks(_doc([_section(7, "content", title="Storage")]))
        assert "[Page 7]" in chunks[0].text
        assert "Storage" in chunks[0].text

    def test_page_range_spans_the_included_sections(self):
        chunks = pv._build_chunks(
            _doc([_section(3, "a"), _section(4, "b"), _section(9, "c")])
        )
        assert chunks[0].page_start == 3
        assert chunks[0].page_end == 9

    def test_an_empty_document_yields_one_empty_chunk(self):
        """Returning [] would make the fan-out evaluate nothing and report a
        vacuous pass."""
        chunks = pv._build_chunks(_doc([]))
        assert len(chunks) == 1
        assert chunks[0].text == ""
        assert chunks[0].page_start == 0

    def test_a_long_document_is_split_into_several_chunks(self):
        big = _doc([_section(i, "x" * 4000) for i in range(1, 40)])
        chunks = pv._build_chunks(big)
        assert len(chunks) > 1
        assert all(len(c.text) <= pv.MAX_CHUNK_CHARS for c in chunks)

    def test_chunk_indexes_are_sequential_from_zero(self):
        big = _doc([_section(i, "x" * 4000) for i in range(1, 40)])
        chunks = pv._build_chunks(big)
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_consecutive_chunks_overlap(self):
        """Overlap stops a requirement that straddles a boundary from being
        invisible to both chunks."""
        big = _doc([_section(i, "x" * 4000) for i in range(1, 40)])
        chunks = pv._build_chunks(big)
        assert len(chunks) >= 2
        tail = chunks[0].text[-pv.CHUNK_OVERLAP_CHARS:]
        assert chunks[1].text.startswith(tail[: min(200, len(tail))])

    def test_every_chunk_gets_a_page_range(self):
        big = _doc([_section(i, "x" * 4000) for i in range(1, 40)])
        for c in pv._build_chunks(big):
            assert c.page_start >= 0 and c.page_end >= c.page_start

    def test_the_hard_document_cap_truncates(self):
        """Beyond the cap, cost grows without bound — one call per (rule, chunk).

        Asserted on the span the chunks cover rather than the sum of their
        lengths: chunks overlap, so the sum legitimately exceeds the cap.
        """
        overshoot = 100_000
        huge = _doc([_section(1, "x" * (pv.MAX_DOC_CHARS + overshoot))])
        chunks = pv._build_chunks(huge)
        step = pv.MAX_CHUNK_CHARS - pv.CHUNK_OVERLAP_CHARS
        covered = step * (len(chunks) - 1) + len(chunks[-1].text)
        assert covered <= pv.MAX_DOC_CHARS
        # And the overshoot really was dropped, not merely re-sliced.
        assert covered < pv.MAX_DOC_CHARS + overshoot


class TestFormatPageRange:
    def test_single_page(self):
        assert pv._format_page_range(7, 7) == "p.7"

    def test_page_range(self):
        assert pv._format_page_range(7, 12) == "pp.7-12"


class TestExtractJson:
    def test_parses_bare_json(self):
        assert pv._extract_json('{"status": "pass"}') == {"status": "pass"}

    def test_parses_fenced_json(self):
        assert pv._extract_json('```json\n{"status": "fail"}\n```')["status"] == "fail"

    def test_ignores_surrounding_prose(self):
        assert pv._extract_json('Here you go:\n{"status": "warning"}')["status"] == "warning"

    @pytest.mark.parametrize("raw", ["", "no json", "{broken", None])
    def test_unparseable_returns_none(self, raw):
        assert pv._extract_json(raw) is None


class TestPartialFromResponse:
    def _chunk(self):
        return pv._Chunk(index=2, text="chunk body " * 100, page_start=3, page_end=5)

    def test_carries_the_model_verdict_through(self):
        p = pv._partial_from_response(
            self._chunk(),
            {
                "status": "fail",
                "evidence": "only one AZ",
                "recommendation": "add a second AZ",
                "affected_components": "rds, alb",
                "source_section": "Storage (p.4)",
            },
            "raw",
        )
        assert p.status == "fail"
        assert p.evidence == "only one AZ"
        assert p.recommendation == "add a second AZ"
        assert p.affected == ["rds", "alb"]
        assert p.source_section == "Storage (p.4)"
        assert p.chunk_index == 2
        assert p.page_start == 3 and p.page_end == 5

    def test_an_unparseable_reply_becomes_an_error_not_a_verdict(self):
        """Mapping this to "pass" or "not_addressed" would invent or hide a
        compliance answer; "error" is what the degraded-pass guard counts."""
        p = pv._partial_from_response(self._chunk(), None, "the model rambled")
        assert p.status == "error"
        assert "could not be parsed" in p.evidence
        assert "rambled" in p.evidence

    def test_an_unknown_status_is_downgraded_to_warning(self):
        """Never trust an unrecognised status as a pass."""
        p = pv._partial_from_response(self._chunk(), {"status": "definitely_fine"}, "r")
        assert p.status == "warning"

    def test_status_is_case_and_whitespace_insensitive(self):
        p = pv._partial_from_response(self._chunk(), {"status": "  FAIL "}, "r")
        assert p.status == "fail"

    def test_a_missing_status_defaults_to_not_addressed(self):
        p = pv._partial_from_response(self._chunk(), {}, "r")
        assert p.status == "not_addressed"

    def test_affected_components_from_a_non_string_are_dropped(self):
        p = pv._partial_from_response(
            self._chunk(), {"status": "fail", "affected_components": {"a": 1}}, "r"
        )
        assert p.affected == []

    def test_long_fields_are_truncated(self):
        p = pv._partial_from_response(
            self._chunk(),
            {"status": "fail", "evidence": "e" * 5000, "recommendation": "r" * 5000},
            "x" * 5000,
        )
        assert len(p.evidence) <= 1500
        assert len(p.recommendation) <= 800
        assert len(p.raw_response) <= 2000

    def test_the_chunk_excerpt_is_capped_for_the_trace(self):
        """The trace is stored per finding; chunks can be 50KB+."""
        p = pv._partial_from_response(self._chunk(), {"status": "pass"}, "r")
        assert len(p.chunk_excerpt) <= 600

    def test_token_counts_and_latency_are_recorded(self):
        p = pv._partial_from_response(
            self._chunk(), {"status": "pass"}, "r",
            input_tokens=120, output_tokens=45, latency_ms=987,
        )
        assert (p.input_tokens, p.output_tokens, p.latency_ms) == (120, 45, 987)

    def test_reasoning_text_is_preserved(self):
        p = pv._partial_from_response(
            self._chunk(), {"status": "pass"}, "r",
            reasoning_text="I checked the AZ table", reasoning_signature="sig",
        )
        assert p.reasoning_text == "I checked the AZ table"
        assert p.reasoning_signature == "sig"

    def test_an_empty_source_section_becomes_none(self):
        p = pv._partial_from_response(
            self._chunk(), {"status": "pass", "source_section": ""}, "r"
        )
        assert p.source_section is None


class TestSourceQuoteBlock:
    def test_includes_the_verbatim_rule_text(self):
        """The strongest grounding signal: the rule's actual wording."""
        block = pv._source_quote_block({"source_quote": "Services MUST span two AZs."})
        assert "MUST span two AZs" in block

    @pytest.mark.parametrize("standard", [{}, {"source_quote": ""}, {"source_quote": "   "}])
    def test_absent_quote_yields_nothing(self, standard):
        assert pv._source_quote_block(standard) == ""

    def test_a_long_quote_is_truncated(self):
        block = pv._source_quote_block({"source_quote": "q" * 2000})
        assert block.count("q") == 800


class TestShouldVerify:
    def test_only_fails_are_verified(self, monkeypatch):
        monkeypatch.setattr(pv, "VERIFY_FAILS_ENABLED", True)
        monkeypatch.setattr(pv, "VERIFY_FAILS_SEVERITIES", {"critical", "high"})
        assert pv._should_verify(_finding(status=ComplianceStatus.FAIL)) is True
        assert pv._should_verify(_finding(status=ComplianceStatus.PASS)) is False
        assert pv._should_verify(_finding(status=ComplianceStatus.WARNING)) is False

    def test_severity_must_be_configured(self, monkeypatch):
        monkeypatch.setattr(pv, "VERIFY_FAILS_ENABLED", True)
        monkeypatch.setattr(pv, "VERIFY_FAILS_SEVERITIES", {"critical"})
        assert pv._should_verify(_finding(severity=Severity.CRITICAL)) is True
        assert pv._should_verify(_finding(severity=Severity.LOW)) is False

    def test_disabled_means_never(self, monkeypatch):
        monkeypatch.setattr(pv, "VERIFY_FAILS_ENABLED", False)
        assert pv._should_verify(_finding(severity=Severity.CRITICAL)) is False


class TestVerifyRefuted:
    def test_refuted_with_high_confidence_overturns(self):
        assert pv._verify_refuted({"refuted": True, "confidence": "high"}) is True

    def test_refuted_with_medium_confidence_overturns(self):
        assert pv._verify_refuted({"refuted": True, "confidence": "medium"}) is True

    def test_a_stringified_boolean_is_accepted(self):
        """Models return "true" as often as true."""
        assert pv._verify_refuted({"refuted": "true", "confidence": "high"}) is True
        assert pv._verify_refuted({"refuted": "TRUE", "confidence": "high"}) is True

    def test_low_confidence_does_not_overturn(self):
        """Strict on confidence: overturning a FAIL on a shaky second opinion
        manufactures a false pass."""
        assert pv._verify_refuted({"refuted": True, "confidence": "low"}) is False

    def test_a_missing_confidence_does_not_overturn(self):
        assert pv._verify_refuted({"refuted": True}) is False

    @pytest.mark.parametrize(
        "verdict",
        [None, {}, {"refuted": False, "confidence": "high"},
         {"refuted": "no", "confidence": "high"}],
    )
    def test_anything_else_keeps_the_fail(self, verdict):
        assert pv._verify_refuted(verdict) is False


class TestApplyVerifyVerdict:
    def test_a_refuted_fail_becomes_a_warning(self):
        f = _finding()
        pv._apply_verify_verdict(
            f, {"refuted": True, "confidence": "high", "rationale": "AZ table on p.4"}
        )
        assert f.status is ComplianceStatus.WARNING
        assert "downgraded to WARNING" in f.evidence
        assert "AZ table on p.4" in f.evidence
        assert "Original: single AZ only" in f.evidence

    def test_the_challenge_is_recorded_in_the_trace(self):
        """Audit trail: a downgrade must be explainable after the fact."""
        f = _finding()
        verdict = {"refuted": True, "confidence": "high", "rationale": "r"}
        pv._apply_verify_verdict(f, verdict)
        assert f.trace["verify_challenge"] == verdict

    def test_a_trace_is_created_when_absent(self):
        f = _finding(trace=None)
        pv._apply_verify_verdict(f, {"refuted": True, "confidence": "high"})
        assert isinstance(f.trace, dict)
        assert "verify_challenge" in f.trace

    def test_an_unrefuted_fail_is_untouched(self):
        f = _finding()
        pv._apply_verify_verdict(f, {"refuted": False, "confidence": "high"})
        assert f.status is ComplianceStatus.FAIL
        assert f.evidence == "single AZ only"

    def test_evidence_stays_within_the_field_limit(self):
        f = _finding(evidence="e" * 1900)
        pv._apply_verify_verdict(
            f, {"refuted": True, "confidence": "high", "rationale": "r" * 400}
        )
        assert len(f.evidence) <= 2000


class TestPassIsUnderminedByErrors:
    def _p(self, status):
        return pv._Partial(
            chunk_index=0, page_start=1, page_end=1, status=status,
            evidence="", recommendation="", affected=[], source_section=None,
        )

    def test_no_errors_is_never_undermined(self):
        passes = [self._p("pass")]
        assert pv._pass_is_undermined_by_errors(passes, [], passes) is False

    def test_a_single_error_among_many_is_tolerated(self):
        """One throttled call on a 20-chunk document does not change the picture."""
        partials = [self._p("pass")] * 19 + [self._p("error")]
        passes = [p for p in partials if p.status == "pass"]
        errors = [p for p in partials if p.status == "error"]
        assert pv._pass_is_undermined_by_errors(passes, errors, partials) is False

    def test_errors_outnumbering_passes_undermines(self):
        partials = [self._p("pass")] + [self._p("error")] * 2
        passes = [p for p in partials if p.status == "pass"]
        errors = [p for p in partials if p.status == "error"]
        assert pv._pass_is_undermined_by_errors(passes, errors, partials) is True

    def test_crossing_the_error_fraction_undermines(self):
        """Past a third of chunks failing, a surviving "pass" covers too little
        of the document to call it compliant."""
        partials = [self._p("pass")] * 6 + [self._p("error")] * 4
        passes = [p for p in partials if p.status == "pass"]
        errors = [p for p in partials if p.status == "error"]
        assert pv._pass_is_undermined_by_errors(passes, errors, partials) is True

    def test_no_partials_at_all_is_not_undermined(self):
        assert pv._pass_is_undermined_by_errors([], [], []) is False


class TestCalculateScore:
    def test_all_passes_is_one_hundred(self):
        assert pv._calculate_score([_finding(status=ComplianceStatus.PASS)]) == 100.0

    def test_all_fails_is_zero(self):
        assert pv._calculate_score([_finding(status=ComplianceStatus.FAIL)]) == 0.0

    def test_no_findings_is_none_not_one_hundred(self):
        """Reporting 100 for a document nothing applied to reads as a perfect
        pass — the pre-fix behaviour, and dangerously misleading."""
        assert pv._calculate_score([]) is None

    def test_all_excluded_is_none(self):
        assert pv._calculate_score([
            _finding(status=ComplianceStatus.NOT_ADDRESSED),
            _finding(status=ComplianceStatus.NOT_APPLICABLE),
        ]) is None

    def test_not_addressed_does_not_drag_the_score_down(self):
        """A coverage gap is not a violation; collapsing it into FAIL drove
        sparse documents to 0."""
        score = pv._calculate_score([
            _finding(status=ComplianceStatus.PASS),
            _finding(status=ComplianceStatus.NOT_ADDRESSED),
        ])
        assert score == 100.0

    def test_an_exempted_rule_is_not_penalised(self):
        score = pv._calculate_score([
            _finding(status=ComplianceStatus.PASS),
            _finding(status=ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION),
        ])
        assert score == 100.0

    def test_a_substitution_pass_earns_full_credit(self):
        assert pv._calculate_score(
            [_finding(status=ComplianceStatus.PASS_VIA_SUBSTITUTION)]
        ) == 100.0

    def test_a_warning_earns_half_credit(self):
        assert pv._calculate_score([_finding(status=ComplianceStatus.WARNING)]) == 50.0

    def test_severity_weights_the_result(self):
        """A critical failure must cost more than a low one."""
        critical_fail = pv._calculate_score([
            _finding(status=ComplianceStatus.FAIL, severity=Severity.CRITICAL),
            _finding(status=ComplianceStatus.PASS, severity=Severity.LOW),
        ])
        low_fail = pv._calculate_score([
            _finding(status=ComplianceStatus.FAIL, severity=Severity.LOW),
            _finding(status=ComplianceStatus.PASS, severity=Severity.CRITICAL),
        ])
        assert critical_fail < low_fail

    def test_the_score_is_rounded_to_one_decimal(self):
        score = pv._calculate_score([
            _finding(status=ComplianceStatus.PASS, severity=Severity.HIGH),
            _finding(status=ComplianceStatus.FAIL, severity=Severity.CRITICAL),
            _finding(status=ComplianceStatus.WARNING, severity=Severity.MEDIUM),
        ])
        assert score == round(score, 1)
        assert 0.0 <= score <= 100.0
