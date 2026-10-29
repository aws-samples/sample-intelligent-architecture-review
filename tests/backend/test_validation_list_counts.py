# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The validations list must report real finding counts.

Findings live in S3, not on the DynamoDB row, so `list_validations` returns
metadata with `findings=[]` on purpose — loading every finding set for a list
view would be one S3 GET per row. The list route then computed
`findings_count`/`critical_count`/`high_count` from that empty list, so every
row reported 0 and the dashboard's "active issues" KPI was permanently 0.

The counts are computed once at save time and stored on the row. These tests
pin that contract: the row carries the counts, the store reads them back, and
the route reports them without touching `findings`.
"""

from __future__ import annotations

import pytest

from app.models.validation import (
    ComplianceStatus,
    Finding,
    Severity,
    ValidationResult,
    ValidationStatus,
)


def _finding(rule_id: str, severity: Severity, status: ComplianceStatus) -> Finding:
    return Finding(
        finding_id=f"f-{rule_id}",
        requirement_id=rule_id,
        requirement_title=f"requirement {rule_id}",
        severity=severity,
        status=status,
        evidence="e",
    )


# One of each severity that fails, plus non-failures that must NOT be counted.
FINDINGS = [
    _finding("c1", Severity.CRITICAL, ComplianceStatus.FAIL),
    _finding("c2", Severity.CRITICAL, ComplianceStatus.FAIL),
    _finding("h1", Severity.HIGH, ComplianceStatus.FAIL),
    _finding("m1", Severity.MEDIUM, ComplianceStatus.FAIL),
    # Not failures: a passing critical rule is not an issue.
    _finding("p1", Severity.CRITICAL, ComplianceStatus.PASS),
    _finding("p2", Severity.HIGH, ComplianceStatus.PASS_VIA_SUBSTITUTION),
    _finding("n1", Severity.HIGH, ComplianceStatus.NOT_ADDRESSED),
    _finding("x1", Severity.CRITICAL, ComplianceStatus.NOT_APPLICABLE_VIA_EXCEPTION),
    # A warning is a real issue but not a fail — counted in findings_count only.
    _finding("w1", Severity.MEDIUM, ComplianceStatus.WARNING),
]


def _result(validation_id: str = "v-counts") -> ValidationResult:
    return ValidationResult(
        validation_id=validation_id,
        document_id="doc-1",
        status=ValidationStatus.COMPLETED,
        standards_set="test",
        findings=list(FINDINGS),
        overall_score=42.0,
    )


class TestCountsOnTheRow:
    def test_save_stamps_the_failing_counts(self):
        from app.services.validation_store import finding_counts

        counts = finding_counts(FINDINGS)
        assert counts["critical_count"] == 2
        assert counts["high_count"] == 1
        # Every failing/warning finding, excluding passes, N/A and not_addressed.
        assert counts["findings_count"] == 5

    def test_passing_findings_are_not_issues(self):
        """A validation where everything passed has no issues to report."""
        from app.services.validation_store import finding_counts

        all_pass = [_finding("p", Severity.CRITICAL, ComplianceStatus.PASS)]
        counts = finding_counts(all_pass)
        assert counts == {"findings_count": 0, "critical_count": 0, "high_count": 0}

    def test_no_findings_is_all_zero(self):
        from app.services.validation_store import finding_counts

        assert finding_counts([]) == {
            "findings_count": 0,
            "critical_count": 0,
            "high_count": 0,
        }


class TestListReadsThemBack:
    @pytest.fixture
    def store(self):
        """In-memory store: the DDB row is simulated by a dict per the
        repo convention (`_available = False`), so the metadata round-trip is
        exercised without AWS."""
        from app.services.validation_store import ValidationStore

        s = ValidationStore()
        s._available = False
        return s

    def test_list_reports_the_counts_without_loading_findings(self, store):
        store.save_validation(_result())
        listed = store.list_validations()
        assert len(listed) == 1
        v = listed[0]
        assert v.findings_count == 2 + 1 + 1 + 1  # critical x2, high, medium, warning
        assert v.critical_count == 2
        assert v.high_count == 1

    def test_route_reports_nonzero_counts(self, store, monkeypatch):
        """The end of the chain: GET /api/validations must not report zeros."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.api.auth import get_current_user
        from app.api.routes.validation import get_validation_store

        store.save_validation(_result())
        app.dependency_overrides[get_current_user] = lambda: {"sub": "s", "email": "u@x.com"}
        app.dependency_overrides[get_validation_store] = lambda: store
        try:
            body = TestClient(app).get("/api/validations").json()
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(get_validation_store, None)

        assert body["total"] == 1
        row = body["validations"][0]
        assert row["critical_count"] == 2, "the dashboard KPI reads this"
        assert row["high_count"] == 1
        assert row["findings_count"] == 5

    def test_ddb_list_reads_the_counts_off_the_row(self):
        """The path that was actually broken in production.

        `list_validations` against DynamoDB rebuilds each ValidationResult from
        the item WITHOUT findings, so the counts have to come off the row. The
        in-memory fallback can't catch a regression here — it returns the cached
        objects, which already carry the counts.
        """
        from app.services.validation_store import ValidationStore

        row = {
            "id": "v-ddb",
            "validation_id": "v-ddb",
            "document_id": "doc-1",
            "status": "completed",
            "standards_set": "test",
            "started_at": "2026-07-28T00:00:00",
            "overall_score": 42.0,
            "findings_count": 5,
            "critical_count": 2,
            "high_count": 1,
        }

        class _Table:
            def scan(self, **kwargs):
                return {"Items": [row]}

        class _Client:
            def Table(self, name):
                return _Table()

        s = ValidationStore()
        s._available = True
        s._client = _Client()

        listed = s.list_validations()
        assert len(listed) == 1
        v = listed[0]
        assert v.findings == [], "list must not pay an S3 GET per row"
        assert v.critical_count == 2
        assert v.high_count == 1
        assert v.findings_count == 5

    def test_counts_survive_a_row_without_them(self, store):
        """Rows written before the counts existed must not crash the list."""
        v = _result("v-legacy")
        store.save_validation(v)
        # Simulate a pre-existing row: drop the count fields entirely.
        cached = store._validations["v-legacy"]
        cached.findings_count = 0
        cached.critical_count = 0
        cached.high_count = 0
        listed = [x for x in store.list_validations() if x.validation_id == "v-legacy"]
        assert listed and listed[0].critical_count == 0
