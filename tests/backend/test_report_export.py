# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Downloadable validation report (CSV + PDF) endpoint tests."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.auth import get_current_user
from app.api.dependencies import get_validation_store
from app.main import app
from app.models.validation import (
    ComplianceStatus,
    Finding,
    Severity,
    ValidationResult,
    ValidationStatus,
)
from app.services.validation_store import ValidationStore


def _finding(status, sev=Severity.HIGH, rid=None):
    return Finding(
        finding_id=f"find-{uuid.uuid4().hex[:8]}",
        requirement_id=rid or "HA-1",
        requirement_title="Multi-AZ",
        severity=sev,
        status=status,
        evidence="line one\nline two",  # newline to verify CSV flattening
        recommendation="deploy multi-AZ",
        affected_components=["RDS", "ALB"],
    )


@pytest.fixture
def store_with_completed():
    store = ValidationStore()
    store._available = False
    result = ValidationResult(
        validation_id="val-exp-1",
        document_id="doc-1",
        status=ValidationStatus.COMPLETED,
        standards_set="default",
        overall_score=42.0,
        summary="2 findings.",
        findings=[
            _finding(ComplianceStatus.FAIL, rid="HA-1"),
            _finding(ComplianceStatus.PASS, rid="HA-2"),
        ],
    )
    store.save_validation(result)
    # A pending one to assert the 400 guard.
    store.save_validation(ValidationResult(
        validation_id="val-pending",
        document_id="doc-1",
        status=ValidationStatus.PENDING,
        standards_set="default",
    ))
    return store


@pytest.fixture
def client(store_with_completed):
    app.dependency_overrides[get_current_user] = lambda: {"sub": "u", "email": "e@x.com"}
    app.dependency_overrides[get_validation_store] = lambda: store_with_completed
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_validation_store, None)


def test_csv_export_downloads_with_headers(client):
    r = client.get("/api/validations/val-exp-1/report.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "val-exp-1.csv" in r.headers["content-disposition"]
    body = r.text
    # Header row + 2 findings.
    assert "requirement_id" in body
    assert "HA-1" in body and "HA-2" in body
    # Newlines in evidence flattened to a single CSV cell line.
    assert "line one line two" in body
    # Components joined.
    assert "RDS; ALB" in body


def test_pdf_export_downloads_with_headers(client):
    r = client.get("/api/validations/val-exp-1/report.pdf")
    # reportlab is a pinned dep; expect a real PDF. (501 only if absent.)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"]
    assert r.content[:5] == b"%PDF-"  # PDF magic


def test_export_404_for_missing(client):
    assert client.get("/api/validations/nope/report.csv").status_code == 404
    assert client.get("/api/validations/nope/report.pdf").status_code == 404


def test_export_rejects_not_completed(client):
    # A non-completed validation must NOT produce a report. In the in-memory
    # store, get_validation intentionally skips cached PENDING entries (the
    # real progress lives in DDB), so it resolves to 404 here; against real
    # DDB a found-but-pending row yields 400. Either way it's rejected — the
    # report is never generated for a non-completed run.
    assert client.get("/api/validations/val-pending/report.csv").status_code in (400, 404)
    assert client.get("/api/validations/val-pending/report.pdf").status_code in (400, 404)
