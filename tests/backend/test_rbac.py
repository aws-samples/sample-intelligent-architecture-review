# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""RBAC — role enforcement tests.

Uses TestClient with auth override. Verifies:
  - require_role blocks the wrong role (403).
  - admin passes everywhere.
  - document upload split: SDD needs validator+, standards needs curator+.
  - bootstrap admin by email.
  - unknown user defaults to viewer.
  - /api/roles/me returns the resolved role.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "boss@x.com")


def _client_as(role: str = "viewer", sub: str = "sub-1", email: str = "u@x.com"):
    """TestClient that injects a fake user with a pre-resolved role via the
    user_roles store (bypassing groups/bootstrap for precise per-test control)."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store

    user_roles_store._available = False
    user_roles_store._mem = {sub: {"sub": sub, "role": role, "email": email}}

    app.dependency_overrides[get_current_user] = lambda: {"sub": sub, "email": email}
    try:
        return TestClient(app)
    finally:
        pass  # caller is responsible for cleanup


@pytest.fixture
def cleanup():
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    yield
    app.dependency_overrides.pop(get_current_user, None)
    user_roles_store._mem = {}


# ---- Enforcement tests ----


def test_curator_can_approve_standard(cleanup):
    c = _client_as("curator")
    # 404 is fine — means the endpoint resolved and tried the action (past auth+rbac).
    r = c.post("/api/standards/HA-D-FAKE/approve")
    assert r.status_code in (404, 409)  # not 403


def test_viewer_blocked_from_approve(cleanup):
    c = _client_as("viewer")
    r = c.post("/api/standards/HA-D-FAKE/approve")
    assert r.status_code == 403


def test_validator_blocked_from_standards_write(cleanup):
    c = _client_as("validator")
    # standards create (POST /api/standards)
    r = c.post("/api/standards", json={
        "name": "x", "description": "x", "severity": "high",
        "category": "a", "validation_criteria": "x",
        "applies_to_tiers": [1], "hosting_scope": "both",
    })
    assert r.status_code == 403


def test_curator_blocked_from_validation(cleanup):
    c = _client_as("curator")
    r = c.post("/api/validations", json={"document_id": "doc-x"})
    assert r.status_code == 403


def test_admin_passes_validation(cleanup):
    c = _client_as("admin")
    r = c.post("/api/validations", json={"document_id": "doc-x"})
    # 404 = past rbac (doc doesn't exist). Not 403.
    assert r.status_code in (404, 400, 500)


def test_admin_passes_standards_write(cleanup):
    c = _client_as("admin")
    r = c.post("/api/standards/HA-D-FAKE/approve")
    assert r.status_code in (404, 409)


def test_doc_upload_sdd_needs_validator(cleanup):
    """SDD upload → validator+ required; curator gets 403."""
    c = _client_as("curator")
    r = c.post("/api/documents?document_type=sdd&hosting_scope=both",
               files={"file": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert r.status_code == 403, f"expected 403 for curator uploading SDD, got {r.status_code}"


def test_doc_upload_standards_needs_curator(cleanup):
    """Standards upload → curator+ required; validator gets 403."""
    c = _client_as("validator")
    r = c.post("/api/documents?document_type=standards&hosting_scope=both",
               files={"file": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert r.status_code == 403, f"expected 403 for validator uploading standards, got {r.status_code}"


def test_doc_upload_sdd_validator_passes(cleanup):
    """Validator can upload an SDD (gets past RBAC; may fail elsewhere)."""
    c = _client_as("validator")
    r = c.post("/api/documents?document_type=sdd&hosting_scope=both",
               files={"file": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")})
    # Anything except 403 means RBAC passed.
    assert r.status_code != 403


# ---- Roles endpoints ----


def test_roles_me_returns_role(cleanup):
    c = _client_as("curator")
    r = c.get("/api/roles/me")
    assert r.status_code == 200
    assert r.json()["role"] == "curator"


def test_roles_list_admin_only(cleanup):
    c = _client_as("viewer")
    r = c.get("/api/roles")
    assert r.status_code == 403


def test_roles_list_admin_passes(cleanup):
    c = _client_as("admin")
    r = c.get("/api/roles")
    assert r.status_code == 200


# ---- Bootstrap + defaults ----


def test_bootstrap_admin_by_email(cleanup):
    """BOOTSTRAP_ADMIN_EMAIL resolves to admin without a DDB row — but only on
    a VERIFIED email, since the address itself is self-assertable."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    user_roles_store._mem = {}  # no rows
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "new-sub", "email": "boss@x.com", "email_verified": True,
    }
    c = TestClient(app)
    r = c.get("/api/roles/me")
    assert r.json()["role"] == "admin"


def test_unknown_user_defaults_viewer(cleanup):
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    user_roles_store._mem = {}
    app.dependency_overrides[get_current_user] = lambda: {"sub": "rando", "email": "nobody@z.com"}
    c = TestClient(app)
    r = c.get("/api/roles/me")
    assert r.json()["role"] == "viewer"


# ---- cognito:groups claim (the path the Cognito RBAC groups activate) ----
#
# The Terraform cognito module creates one group per role (name == role value).
# Group membership surfaces as a `cognito:groups` claim on the token, which
# resolve_role reads FIRST — ahead of the user-roles table and bootstrap.
# These tests lock the group-name↔role contract and the priority ordering so a
# future rename of a group (or the enum) can't silently break federation.


@pytest.mark.parametrize("role", ["admin", "validator", "curator", "viewer"])
def test_group_claim_resolves_to_named_role(cleanup, role):
    """Each Cognito group name maps to its role — the exact contract the
    Terraform group names rely on."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    user_roles_store._mem = {}  # no DDB row — only the group claim is present
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "g-sub", "email": "g@x.com", "cognito:groups": [role],
        # Only a federated token's groups claim is trusted (see rbac).
        "identities": [{"providerName": "MockSAML"}],
    }
    c = TestClient(app)
    assert c.get("/api/roles/me").json()["role"] == role


def test_group_claim_picks_highest_privilege(cleanup):
    """A user in multiple groups resolves to the highest-privilege one."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    user_roles_store._mem = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "g-sub", "email": "g@x.com",
        "cognito:groups": ["viewer", "curator", "admin"],
        "identities": [{"providerName": "MockSAML"}],
    }
    c = TestClient(app)
    assert c.get("/api/roles/me").json()["role"] == "admin"


def test_group_claim_wins_over_table_row(cleanup):
    """The groups claim takes priority over the user-roles table: a user whose
    DDB row says viewer but whose token carries the curator group is curator."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    user_roles_store._mem = {"g-sub": {"sub": "g-sub", "role": "viewer", "email": "g@x.com"}}
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "g-sub", "email": "g@x.com", "cognito:groups": ["curator"],
        "identities": [{"providerName": "MockSAML"}],
    }
    c = TestClient(app)
    assert c.get("/api/roles/me").json()["role"] == "curator"


def test_unknown_group_falls_through_to_table(cleanup):
    """Groups that aren't role names are ignored; resolution falls through to
    the DDB row (so an Entra group not yet mapped can't grant access)."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    user_roles_store._mem = {"g-sub": {"sub": "g-sub", "role": "validator", "email": "g@x.com"}}
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "g-sub", "email": "g@x.com",
        "cognito:groups": ["Domain Admins", "SomeEntraGroup"],
    }
    c = TestClient(app)
    assert c.get("/api/roles/me").json()["role"] == "validator"


# ---- privilege-escalation defences ----------------------------------------
#
# A review found two remote paths to admin: Cognito self-signup was open, the
# web client could write its own custom:saml_groups (which the pre-token Lambda
# turns into the cognito:groups claim RBAC trusts first), and the bootstrap-admin
# check matched an unverified, self-assertable email. The Terraform side is
# closed; these tests hold the code-side defences in place.

def test_group_claim_is_ignored_on_a_native_token(cleanup):
    """The groups claim is only meaningful when an IdP asserted it.

    On a native username/password login there is no federated identity, so a
    user who can write their own attributes must not be able to name their own
    role. Such a token falls through to the role table instead.
    """
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    user_roles_store._mem = {}  # no row -> should land on the safe default
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "native-sub", "email": "attacker@x.com",
        "cognito:groups": ["admin"],   # self-granted, no "identities" claim
    }
    c = TestClient(app)
    assert c.get("/api/roles/me").json()["role"] == "viewer"


def test_native_token_still_uses_the_role_table(cleanup):
    """Closing the claim path must not break the seeded password logins, which
    resolve their role from the user-roles table."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    user_roles_store._mem = {
        "seeded": {"sub": "seeded", "role": "curator", "email": "curator@x.com"}
    }
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "seeded", "email": "curator@x.com",
        "cognito:groups": ["admin"],   # claim ignored; the table decides
    }
    c = TestClient(app)
    assert c.get("/api/roles/me").json()["role"] == "curator"


def test_bootstrap_admin_requires_a_verified_email(cleanup):
    """Matching the bootstrap address grants admin, so an unverified (i.e.
    self-asserted) email must not qualify."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    user_roles_store._mem = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "imposter", "email": "boss@x.com",  # the bootstrap address...
        "email_verified": False,                    # ...but unproven
    }
    c = TestClient(app)
    assert c.get("/api/roles/me").json()["role"] == "viewer"


def test_bootstrap_admin_rejects_a_missing_email_verified_claim(cleanup):
    """Absent means unproven — don't assume good faith on a claim that gates
    admin."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    user_roles_store._available = False
    user_roles_store._mem = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "imposter", "email": "boss@x.com",  # no email_verified at all
    }
    c = TestClient(app)
    assert c.get("/api/roles/me").json()["role"] == "viewer"
