# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Audit trail — store + endpoint tests (in-memory mode)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "boss@x.com")


def _fresh_store():
    from app.services.audit_store import AuditStore
    s = AuditStore()
    s._available = False  # force in-memory
    s._mem = []
    return s


def test_record_and_list_for_entity():
    s = _fresh_store()
    s.record(action="standard.approve", entity_type="standard", entity_id="HA-1",
             actor_sub="sub-1", actor_email="a@x.com", actor_role="curator")
    s.record(action="standard.edit", entity_type="standard", entity_id="HA-1",
             actor_sub="sub-1", actor_email="a@x.com")
    s.record(action="standard.approve", entity_type="standard", entity_id="HA-2",
             actor_sub="sub-2", actor_email="b@x.com")

    hist = s.list_for_entity("standard", "HA-1")
    assert len(hist) == 2
    assert {e["action"] for e in hist} == {"standard.approve", "standard.edit"}
    # newest first
    assert hist[0]["ts"] >= hist[1]["ts"]


def test_list_for_actor():
    s = _fresh_store()
    s.record(action="document.upload", entity_type="document", entity_id="doc-1",
             actor_sub="sub-1", actor_email="a@x.com")
    s.record(action="validation.create", entity_type="validation", entity_id="val-1",
             actor_sub="sub-1", actor_email="a@x.com")
    s.record(action="document.upload", entity_type="document", entity_id="doc-2",
             actor_sub="sub-2", actor_email="b@x.com")

    acts = s.list_for_actor("sub-1")
    assert len(acts) == 2
    assert all(e["actor_sub"] == "sub-1" for e in acts)


def test_record_from_claims_extracts_actor():
    s = _fresh_store()
    row = s.record_from_claims(
        {"sub": "sub-9", "email": "c@x.com", "_role": "admin"},
        action="standard.delete", entity_type="standard", entity_id="HA-9",
    )
    assert row["actor_sub"] == "sub-9"
    assert row["actor_email"] == "c@x.com"
    assert row["actor_role"] == "admin"


def test_record_never_raises_on_bad_input():
    s = _fresh_store()
    # Missing claims -> unattributed but still recorded (actor_sub sentinel).
    row = s.record_from_claims(None, action="x.y", entity_type="t", entity_id="i")
    assert row is not None
    assert row["actor_sub"] == "unknown"


def test_audit_endpoint_requires_a_mode(cleanup_audit):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {"sub": "s", "email": "e@x.com"}
    c = TestClient(app)
    # No params -> 400
    assert c.get("/api/audit").status_code == 400
    # entity mode -> 200 + events list
    r = c.get("/api/audit?entity_type=document&entity_id=doc-1")
    assert r.status_code == 200
    assert "events" in r.json()


@pytest.fixture
def cleanup_audit():
    from app.main import app
    from app.api.auth import get_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)
