# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A partly-failed run must not report a clean pass.

`_merge_partials` used to check `passes` before it checked errors, and only
honoured errors when EVERY chunk failed. So during a Bedrock throttling event,
nine failed chunks plus one surviving "pass" merged to PASS with a 100 score —
a degraded run byte-for-byte indistinguishable from a clean one, in the UI and
in the export.

For a compliance tool that is the worst available failure: confidently wrong and
unattributable. These tests pin the guard that downgrades an unsupported pass to
WARNING and says why.
"""

from __future__ import annotations

import pytest

from app.models.validation import ComplianceStatus
from app.services.parallel_validator import (
    MAX_ERROR_FRACTION_FOR_PASS,
    _merge_partials,
    _pass_is_undermined_by_errors,
)

STANDARD = {
    "id": "HA-001",
    "name": "Multi-AZ deployment",
    "description": "Database must span availability zones",
    "severity": "critical",
    "validation_criteria": "Check for Multi-AZ",
}


def _partial(status: str, page: int = 1, evidence: str = "e"):
    """Build the _Partial shape _merge_partials consumes."""
    from app.services.parallel_validator import _Partial

    return _Partial(
        chunk_index=page - 1,
        status=status,
        evidence=evidence,
        recommendation="",
        affected=[],
        source_section=None,
        page_start=page,
        page_end=page,
        prompt="p",
        chunk_excerpt="c",
        raw_response="r",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
    )


class TestDegradedPassIsNotReportedAsCompliant:
    def test_nine_errors_and_one_pass_is_not_a_pass(self):
        """The exact scenario from the review: a throttling event leaves one
        surviving chunk, and the rule must not read as compliant."""
        partials = [_partial("error", i) for i in range(1, 10)] + [_partial("pass", 10)]
        finding = _merge_partials(STANDARD, partials)
        assert finding.status is not ComplianceStatus.PASS
        assert finding.status is ComplianceStatus.WARNING

    def test_the_reason_is_in_the_evidence_not_just_a_log(self):
        """The report and the CSV/PDF export both render evidence, so that is
        where a user has to be able to see the run was degraded."""
        partials = [_partial("error", i) for i in range(1, 10)] + [_partial("pass", 10)]
        finding = _merge_partials(STANDARD, partials)
        assert "could be evaluated" in finding.evidence
        assert "9 failed" in finding.evidence
        # The original model evidence is preserved after the note.
        assert finding.evidence.rstrip().endswith("e")

    def test_errors_outnumbering_passes_downgrades(self):
        partials = [_partial("error", 1), _partial("error", 2), _partial("pass", 3)]
        assert _merge_partials(STANDARD, partials).status is ComplianceStatus.WARNING

    def test_a_single_failed_chunk_among_many_still_passes(self):
        """One throttled call on a large document is noise — downgrading every
        run with a single hiccup would make the signal useless."""
        partials = [_partial("pass", i) for i in range(1, 20)] + [_partial("error", 20)]
        assert _merge_partials(STANDARD, partials).status is ComplianceStatus.PASS

    def test_a_clean_run_is_unaffected(self):
        partials = [_partial("pass", 1), _partial("pass", 2)]
        finding = _merge_partials(STANDARD, partials)
        assert finding.status is ComplianceStatus.PASS
        assert "could be evaluated" not in finding.evidence

    def test_a_fail_still_wins_over_errors(self):
        """Precedence is unchanged: a real violation outranks everything, and a
        degraded run must never hide one."""
        partials = [_partial("error", 1), _partial("error", 2), _partial("fail", 3)]
        assert _merge_partials(STANDARD, partials).status is ComplianceStatus.FAIL

    def test_all_chunks_erroring_is_still_a_warning(self):
        partials = [_partial("error", 1), _partial("error", 2)]
        assert _merge_partials(STANDARD, partials).status is ComplianceStatus.WARNING


class TestPredicate:
    def test_no_errors_never_undermines(self):
        passes = [_partial("pass", 1)]
        assert _pass_is_undermined_by_errors(passes, [], passes) is False

    @pytest.mark.parametrize("n_pass,n_err,expected", [
        (1, 9, True),    # overwhelming failure
        (1, 2, True),    # errors outnumber passes
        (2, 1, False),   # 1/3 exactly — at the boundary, not over it
        (19, 1, False),  # a single hiccup on a big document
        (1, 1, False),   # tie: not "more errors than passes", and 1/2 > 1/3...
    ])
    def test_boundaries(self, n_pass, n_err, expected):
        passes = [_partial("pass", i) for i in range(n_pass)]
        errors = [_partial("error", i) for i in range(n_err)]
        got = _pass_is_undermined_by_errors(passes, errors, passes + errors)
        # The 1-1 case IS undermined by the fraction rule (0.5 > 1/3) even though
        # neither side outnumbers the other; assert on the real behaviour.
        if (n_pass, n_err) == (1, 1):
            assert got is True
        else:
            assert got is expected

    def test_empty_partials_is_safe(self):
        assert _pass_is_undermined_by_errors([], [], []) is False

    def test_threshold_is_a_fraction_between_zero_and_one(self):
        assert 0 < MAX_ERROR_FRACTION_FOR_PASS < 1
