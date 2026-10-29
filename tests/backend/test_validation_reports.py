# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Validation report endpoints: JSON, CSV download, PDF download.

These are what a user hands to an auditor, so the failure modes are about
exported content rather than crashes: a CSV whose embedded newlines break row
alignment, a report served for a half-finished run, or a PDF route that 500s
opaquely when reportlab is missing instead of pointing at the CSV.

Reports are refused for non-completed validations on purpose — exporting a
partial run as if it were a finished audit is worse than refusing.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.models.validation import (
    ComplianceStatus,
    Finding,
    Severity,
    ValidationResult,
    ValidationStatus,
)


def _finding(fid="f1", **over) -> Finding:
    base = dict(
        finding_id=fid,
        requirement_id="HA-001",
        requirement_title="Multi-AZ deployment",
        severity=Severity.CRITICAL,
        status=ComplianceStatus.FAIL,
        evidence="Only one availability zone is described.",
        recommendation="Deploy across two AZs.",
        affected_components=["rds", "alb"],
        source_section="Storage (p.4)",
    )
    base.update(over)
    return Finding(**base)


def _validation(status=ValidationStatus.COMPLETED, findings=None, **over) -> ValidationResult:
    base = dict(
        validation_id="val-1",
        document_id="doc-1",
        status=status,
        standards_set="default",
        overall_score=42.5,
        summary="Two critical gaps in the storage layer.",
        findings=findings if findings is not None else [_finding()],
    )
    base.update(over)
    return ValidationResult(**base)


class _Store:
    def __init__(self, validation=None):
        self.validation = validation

    def get_validation(self, validation_id):
        return self.validation

    def get_document(self, document_id):
        return None

    def list_standards(self, **kw):
        return []


@pytest.fixture
def client():
    from app.main import app
    from app.api.auth import get_current_user
    from app.api.dependencies import get_validation_store

    def _make(validation=None):
        app.dependency_overrides[get_current_user] = lambda: {
            "sub": "s", "email": "u@x.com"
        }
        app.dependency_overrides[get_validation_store] = lambda: _Store(validation)
        return TestClient(app)

    yield _make

    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_validation_store, None)


class TestJsonReport:
    def test_returns_the_report_for_a_completed_run(self, client):
        resp = client(_validation()).get("/api/validations/val-1/report")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["validation_id"] == "val-1"
        assert body["overall_score"] == 42.5
        assert body["executive_summary"].startswith("Two critical gaps")

    def test_statistics_count_each_status(self, client):
        v = _validation(findings=[
            _finding("a", status=ComplianceStatus.PASS),
            _finding("b", status=ComplianceStatus.FAIL),
            _finding("c", status=ComplianceStatus.FAIL),
            _finding("d", status=ComplianceStatus.WARNING),
            _finding("e", status=ComplianceStatus.NOT_ADDRESSED),
        ])
        stats = client(v).get("/api/validations/val-1/report").json()["statistics"]
        assert stats["total_requirements"] == 5
        assert stats["passed"] == 1
        assert stats["failed"] == 2
        assert stats["warnings"] == 1

    def test_a_missing_summary_falls_back_to_a_message(self, client):
        v = _validation(summary=None)
        body = client(v).get("/api/validations/val-1/report").json()
        assert body["executive_summary"] == "No summary available."

    def test_unknown_validation_is_404(self, client):
        assert client(None).get("/api/validations/val-1/report").status_code == 404

    @pytest.mark.parametrize(
        "status",
        [ValidationStatus.PENDING, ValidationStatus.FAILED, ValidationStatus.CANCELLED],
    )
    def test_an_incomplete_run_is_refused(self, client, status):
        """Exporting a partial run as a finished audit is worse than refusing."""
        resp = client(_validation(status=status)).get("/api/validations/val-1/report")
        assert resp.status_code == 400


class TestCsvDownload:
    def _rows(self, resp):
        return list(csv.DictReader(io.StringIO(resp.text)))

    def test_one_row_per_finding(self, client):
        v = _validation(findings=[_finding("a"), _finding("b"), _finding("c")])
        resp = client(v).get("/api/validations/val-1/report.csv")
        assert resp.status_code == 200, resp.text
        assert len(self._rows(resp)) == 3

    def test_served_as_a_download_with_a_filename(self, client):
        """Without Content-Disposition the browser renders CSV inline instead of
        saving it."""
        resp = client(_validation()).get("/api/validations/val-1/report.csv")
        assert resp.headers["content-type"].startswith("text/csv")
        assert 'attachment; filename="validation-val-1.csv"' in (
            resp.headers["content-disposition"]
        )

    def test_the_header_matches_the_declared_columns(self, client):
        from app.api.routes.validation import _CSV_COLUMNS

        resp = client(_validation()).get("/api/validations/val-1/report.csv")
        assert self._rows(resp)[0].keys() == set(_CSV_COLUMNS) or list(
            csv.reader(io.StringIO(resp.text))
        )[0] == _CSV_COLUMNS

    def test_finding_fields_are_written(self, client):
        row = self._rows(client(_validation()).get("/api/validations/val-1/report.csv"))[0]
        assert row["requirement_id"] == "HA-001"
        assert row["requirement_title"] == "Multi-AZ deployment"
        assert row["severity"] == "critical"
        assert row["status"] == "fail"
        assert "one availability zone" in row["evidence"]

    def test_components_are_joined_into_one_cell(self, client):
        row = self._rows(client(_validation()).get("/api/validations/val-1/report.csv"))[0]
        assert row["affected_components"] == "rds; alb"

    def test_newlines_in_evidence_are_flattened(self, client):
        """A raw newline inside a quoted cell survives RFC-4180 parsing, but
        breaks every naive spreadsheet import — so they are stripped."""
        v = _validation(findings=[
            _finding(evidence="line one\nline two", recommendation="do\nthis")
        ])
        resp = client(v).get("/api/validations/val-1/report.csv")
        row = self._rows(resp)[0]
        assert "\n" not in row["evidence"]
        assert "line one line two" == row["evidence"]
        assert "\n" not in row["recommendation"]

    def test_commas_in_content_do_not_break_columns(self, client):
        v = _validation(findings=[_finding(evidence="a, b, and c")])
        row = self._rows(client(v).get("/api/validations/val-1/report.csv"))[0]
        assert row["evidence"] == "a, b, and c"
        assert row["requirement_id"] == "HA-001"  # columns still aligned

    def test_optional_fields_render_as_empty_not_none(self, client):
        """The literal string "None" in an audit export is a defect."""
        v = _validation(findings=[
            _finding(rule_type=None, via_rule_id=None, source_section=None,
                     recommendation="")
        ])
        row = self._rows(client(v).get("/api/validations/val-1/report.csv"))[0]
        assert row["rule_type"] == ""
        assert row["via_rule_id"] == ""
        assert row["source_section"] == ""
        assert "None" not in "|".join(f"{k}={v}" for k, v in row.items())

    def test_decision_tree_fields_are_exported(self, client):
        v = _validation(findings=[
            _finding(status=ComplianceStatus.PASS_VIA_SUBSTITUTION,
                     rule_type="substitution", via_rule_id="SUB-9")
        ])
        row = self._rows(client(v).get("/api/validations/val-1/report.csv"))[0]
        assert row["rule_type"] == "substitution"
        assert row["via_rule_id"] == "SUB-9"
        assert row["status"] == "pass_via_substitution"

    def test_a_run_with_no_findings_still_yields_a_header(self, client):
        resp = client(_validation(findings=[])).get("/api/validations/val-1/report.csv")
        assert resp.status_code == 200
        assert "requirement_id" in resp.text
        assert self._rows(resp) == []

    def test_unknown_validation_is_404(self, client):
        assert client(None).get("/api/validations/val-1/report.csv").status_code == 404

    def test_an_incomplete_run_is_refused(self, client):
        resp = client(_validation(status=ValidationStatus.PENDING)).get(
            "/api/validations/val-1/report.csv"
        )
        assert resp.status_code == 400



class TestPdfDownload:
    def test_returns_a_pdf(self, client):
        resp = client(_validation()).get("/api/validations/val-1/report.pdf")
        if resp.status_code == 501:
            pytest.skip("reportlab not installed in this environment")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_served_as_a_download(self, client):
        resp = client(_validation()).get("/api/validations/val-1/report.pdf")
        if resp.status_code == 501:
            pytest.skip("reportlab not installed in this environment")
        assert "attachment" in resp.headers["content-disposition"]
        assert "validation-val-1.pdf" in resp.headers["content-disposition"]

    def test_a_missing_renderer_points_at_the_csv(self, client, monkeypatch):
        """501 with a usable alternative beats an opaque 500."""
        import app.api.routes.validation as mod

        def _no_reportlab(*a, **kw):
            raise ImportError("no reportlab")

        monkeypatch.setattr(mod, "_render_validation_pdf", _no_reportlab)
        resp = client(_validation()).get("/api/validations/val-1/report.pdf")
        assert resp.status_code == 501
        assert "report.csv" in resp.json()["detail"]

    def test_unknown_validation_is_404(self, client):
        assert client(None).get("/api/validations/val-1/report.pdf").status_code == 404

    def test_an_incomplete_run_is_refused(self, client):
        resp = client(_validation(status=ValidationStatus.CANCELLED)).get(
            "/api/validations/val-1/report.pdf"
        )
        assert resp.status_code == 400


class TestRequireCompleted:
    def test_returns_the_validation_when_completed(self):
        from app.api.routes.validation import _require_completed

        v = _validation()
        assert _require_completed(_Store(v), "val-1") is v

    def test_raises_404_when_absent(self):
        from fastapi import HTTPException
        from app.api.routes.validation import _require_completed

        with pytest.raises(HTTPException) as exc:
            _require_completed(_Store(None), "val-1")
        assert exc.value.status_code == 404

    def test_raises_400_when_incomplete(self):
        from fastapi import HTTPException
        from app.api.routes.validation import _require_completed

        with pytest.raises(HTTPException) as exc:
            _require_completed(_Store(_validation(status=ValidationStatus.PENDING)), "val-1")
        assert exc.value.status_code == 400
        assert "completed" in exc.value.detail.lower()


class TestEnumStr:
    def test_unwraps_an_enum(self):
        from app.api.routes.validation import _enum_str

        assert _enum_str(Severity.CRITICAL) == "critical"

    def test_passes_a_plain_string_through(self):
        from app.api.routes.validation import _enum_str

        assert _enum_str("fail") == "fail"

    def test_none_becomes_empty(self):
        from app.api.routes.validation import _enum_str

        assert _enum_str(None) == ""
