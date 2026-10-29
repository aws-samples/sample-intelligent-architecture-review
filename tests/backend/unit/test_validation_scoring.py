# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Compliance score calculation.

These tests call the PRODUCTION scorer. They previously exercised a copy of the
logic pasted into this file, which had since diverged from the real one in four
ways — including scoring a substitution-satisfied rule 0 instead of 100, and
returning 0.0 for a document with nothing evaluable where production
deliberately returns None. Six tests were green against behaviour the product
does not have, so the copy is gone: import the real thing or don't test it.
"""

import pytest

from app.models.validation import ComplianceStatus, Finding, Severity
from app.services.parallel_validator import _calculate_score as calculate_score


def _finding(status: ComplianceStatus, severity: Severity = Severity.HIGH,
             requirement_id: str = "HA-001") -> Finding:
    return Finding(
        finding_id=f"f-{requirement_id}-{status.value}",
        requirement_id=requirement_id,
        requirement_title="Test requirement",
        severity=severity,
        status=status,
        evidence="evidence",
        recommendation="",
    )


class TestFullCredit:
    """Statuses that mean "the requirement is met"."""

    @pytest.mark.unit
    def test_all_pass_returns_100(self):
        findings = [
            _finding(ComplianceStatus.PASS, Severity.CRITICAL, "HA-001"),
            _finding(ComplianceStatus.PASS, Severity.HIGH, "HA-002"),
        ]
        assert calculate_score(findings) == 100.0

    @pytest.mark.unit
    def test_substitution_earns_full_credit(self):
        """An approved alternative architecture satisfies the founding rule, so
        it must score like a pass — not zero. The removed local copy had no
        branch for this status and silently scored it 0."""
        findings = [_finding(ComplianceStatus.PASS_VIA_SUBSTITUTION, Severity.CRITICAL)]
        assert calculate_score(findings) == 100.0

    @pytest.mark.unit
    def test_substitution_and_pass_together_stay_100(self):
        findings = [
            _finding(ComplianceStatus.PASS, Severity.CRITICAL, "HA-001"),
            _finding(ComplianceStatus.PASS_VIA_SUBSTITUTION, Severity.HIGH, "HA-002"),
        ]
        assert calculate_score(findings) == 100.0


class TestNoCredit:
    @pytest.mark.unit
    def test_all_fail_returns_0(self):
        findings = [
            _finding(ComplianceStatus.FAIL, Severity.CRITICAL, "HA-001"),
            _finding(ComplianceStatus.FAIL, Severity.HIGH, "HA-002"),
        ]
        assert calculate_score(findings) == 0.0

    @pytest.mark.unit
    def test_warning_earns_half_credit(self):
        """A WARNING is an unproven verdict (often a model/Bedrock failure), so
        it earns partial credit rather than counting as a pass."""
        findings = [_finding(ComplianceStatus.WARNING, Severity.HIGH)]
        assert calculate_score(findings) == 50.0


class TestExcludedFromScoring:
    """Statuses that must not drag the score down, because they say nothing
    about compliance — they say the rule didn't apply or wasn't assessed."""

    @pytest.mark.unit
    def test_not_addressed_is_excluded_from_the_denominator(self):
        """One pass and one unevaluable rule is 100%, not 50% — the removed
        copy counted NOT_ADDRESSED against the document."""
        findings = [
            _finding(ComplianceStatus.PASS, Severity.HIGH, "HA-001"),
            _finding(ComplianceStatus.NOT_ADDRESSED, Severity.HIGH, "HA-002"),
        ]
        assert calculate_score(findings) == 100.0

    @pytest.mark.unit
    def test_exception_is_excluded_from_the_denominator(self):
        findings = [
            _finding(ComplianceStatus.PASS, Severity.HIGH, "HA-001"),
            _finding(ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION, Severity.CRITICAL,
                     "HA-002"),
        ]
        assert calculate_score(findings) == 100.0


class TestNoEvaluableRules:
    """The important boundary: a document nobody could judge has NO score.

    Reporting 0 would read as "failed everything" and reporting 100 as "passed
    everything"; both are lies about a run that evaluated nothing. Production
    returns None so the UI can say so.
    """

    @pytest.mark.unit
    def test_empty_findings_returns_none(self):
        assert calculate_score([]) is None

    @pytest.mark.unit
    def test_all_not_addressed_returns_none(self):
        findings = [
            _finding(ComplianceStatus.NOT_ADDRESSED, Severity.HIGH, "HA-001"),
            _finding(ComplianceStatus.NOT_ADDRESSED, Severity.CRITICAL, "HA-002"),
        ]
        assert calculate_score(findings) is None


class TestSeverityWeighting:
    @pytest.mark.unit
    def test_critical_failure_costs_more_than_a_low_one(self):
        """Weights are what make the score meaningful; equal weighting would let
        a critical breach hide behind a pile of passing trivia."""
        critical_fail = calculate_score([
            _finding(ComplianceStatus.FAIL, Severity.CRITICAL, "HA-001"),
            _finding(ComplianceStatus.PASS, Severity.LOW, "HA-002"),
        ])
        low_fail = calculate_score([
            _finding(ComplianceStatus.FAIL, Severity.LOW, "HA-001"),
            _finding(ComplianceStatus.PASS, Severity.CRITICAL, "HA-002"),
        ])
        assert critical_fail < low_fail

    @pytest.mark.unit
    def test_mixed_pass_fail_is_between_the_extremes(self):
        score = calculate_score([
            _finding(ComplianceStatus.PASS, Severity.HIGH, "HA-001"),
            _finding(ComplianceStatus.FAIL, Severity.HIGH, "HA-002"),
        ])
        assert score == 50.0
