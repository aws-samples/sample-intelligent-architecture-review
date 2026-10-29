# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E2E: the deployed API is up, authenticated, and enforcing roles.

Cheap, read-only smoke checks — no model spend. Run first after a deploy.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_health_endpoint_is_open(anonymous):
    """Health must answer without auth so the ALB target group can poll it."""
    resp = anonymous.get("/api/health")
    assert resp.status_code == 200, resp.text


def test_protected_endpoint_rejects_anonymous(anonymous):
    resp = anonymous.get("/api/standards")
    assert resp.status_code in (401, 403), (
        f"unauthenticated caller got {resp.status_code}; the API must not be open"
    )


def test_openapi_is_served(anonymous):
    # Under /api so CloudFront routes it to the backend origin — the ALB now
    # refuses direct traffic, and only /api/* reaches this app through the CDN.
    resp = anonymous.get("/api/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    # Spot-check the surface the frontend depends on.
    for path in (
        "/api/health",
        "/api/documents",
        "/api/validations",
        "/api/standards",
        "/api/icon-legend",
        "/api/roles/me",
    ):
        assert path in paths, f"{path} missing from the deployed API"


def test_roles_resolve_for_each_seeded_user(curator, viewer):
    assert curator.get("/api/roles/me").json()["role"] == "curator"
    assert viewer.get("/api/roles/me").json()["role"] == "viewer"


def test_viewer_cannot_write_standards(viewer):
    """Separation of duties: a viewer is read-only."""
    resp = viewer.post("/api/standards", json={
        "name": "e2e-should-not-exist",
        "description": "attempt by a viewer",
        "severity": "low",
        "category": "test",
        "validation_criteria": "n/a",
        "hosting_scope": "both",
    })
    assert resp.status_code == 403, resp.text


def test_curator_cannot_trigger_validation(curator):
    """Curators own the library; running validations is the validator's job."""
    resp = curator.post("/api/validations", json={
        "document_id": "e2e-nonexistent",
        "standards_set": "default",
    })
    assert resp.status_code == 403, resp.text
