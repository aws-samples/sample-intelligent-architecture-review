# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The public health endpoint must not describe the infrastructure.

/health is unauthenticated by necessity — the ALB target group and the frontend's
connectivity indicator both poll it. It used to return the S3 bucket name, the
DynamoDB table name, the Bedrock model id, the Knowledge Base and data-source
ids, the region, and raw exception text from each dependency probe. Since the ALB
is internet-facing, that was a free reconnaissance map of the account for anyone
who found the DNS name.

The detail moved to /api/health/detail behind admin auth. These tests keep the
public probe boring.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def anon_client():
    """No auth override — /health must work with no credentials at all."""
    from app.main import app
    return TestClient(app)


def _client_as(role: str) -> TestClient:
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store

    user_roles_store._available = False
    user_roles_store._mem = {"s": {"sub": "s", "role": role, "email": "u@x.com"}}
    app.dependency_overrides[get_current_user] = lambda: {"sub": "s", "email": "u@x.com"}
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides.pop(get_current_user, None)


# Values that must never appear in an unauthenticated response. Derived from
# settings so the test tracks config rather than hardcoding this deployment's.
def _secret_ish_values() -> dict[str, str]:
    from app.config import settings
    out = {
        "s3 bucket": settings.s3_documents_bucket,
        "documents table": settings.documents_table,
        "table prefix": settings.dynamodb_table_prefix,
        "bedrock model": settings.bedrock_model_id,
    }
    if settings.knowledge_base_id:
        out["knowledge base id"] = settings.knowledge_base_id
    if settings.data_source_id:
        out["data source id"] = settings.data_source_id
    return {k: v for k, v in out.items() if v}


class TestPublicProbe:
    @pytest.mark.parametrize("path", ["/health", "/api/health"])
    def test_reachable_without_credentials(self, anon_client, path):
        """The ALB health check and the frontend indicator both need this open."""
        resp = anon_client.get(path)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"

    @pytest.mark.parametrize("path", ["/health", "/api/health"])
    def test_leaks_no_infrastructure_identifiers(self, anon_client, path):
        body = anon_client.get(path).text
        leaked = [
            label for label, value in _secret_ish_values().items()
            if value and value in body
        ]
        assert not leaked, (
            f"{path} exposes infrastructure detail to an unauthenticated "
            f"caller: {leaked}. Move it to /api/health/detail."
        )

    @pytest.mark.parametrize("path", ["/health", "/api/health"])
    def test_exposes_no_dependency_probe_results(self, anon_client, path):
        """Which dependencies exist, and whether they're failing, is itself
        useful to an attacker — and the error branches carried ARNs."""
        body = anon_client.get(path).json()
        assert "dependencies" not in body
        assert "config" not in body


class TestDetailedProbe:
    def test_admin_gets_the_detail(self):
        resp = _client_as("admin").get("/api/health/detail")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "dependencies" in body and "config" in body

    @pytest.mark.parametrize("role", ["viewer", "validator", "curator"])
    def test_non_admins_are_refused(self, role):
        assert _client_as(role).get("/api/health/detail").status_code == 403

    def test_unauthenticated_is_refused(self, anon_client):
        assert anon_client.get("/api/health/detail").status_code in (401, 403)

    def test_dependency_errors_report_a_type_not_raw_text(self, monkeypatch):
        """Raw exception text can carry ARNs and role names; even for an admin,
        report the exception type and leave the detail in the task logs."""
        import app.main as main_mod

        class _Boom:
            def __init__(self, *a, **kw):
                pass

            def describe_table(self, **kw):
                raise RuntimeError("arn:aws:iam::123456789012:role/secret-role")

            def head_bucket(self, **kw):
                raise RuntimeError("arn:aws:iam::123456789012:role/secret-role")

        monkeypatch.setattr(main_mod, "__name__", main_mod.__name__)  # no-op guard
        import boto3
        monkeypatch.setattr(boto3, "client", lambda *a, **kw: _Boom())

        body = _client_as("admin").get("/api/health/detail").text
        assert "secret-role" not in body
        assert "RuntimeError" in body
