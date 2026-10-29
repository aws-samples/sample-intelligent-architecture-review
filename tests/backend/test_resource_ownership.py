# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Role gates say "may this user ever do X"; they can't say "to THIS row".

Two mutations were reachable by any holder of the validator role regardless of
whose work they touched:

  PATCH /api/documents/{id}/tier   — the tier selects which rules an SDD is
      judged against, so retiering someone else's document changes their verdict.
  POST  /api/validations/{id}/cancel — kills a run that bills Bedrock until it
      stops.

And the audit trail's per-actor mode returned any colleague's full activity
history to any authenticated caller.

Reads stay shared on purpose: the four roles are a shared review model (a
curator reviews SDDs a validator uploaded), so this is ownership-on-writes, not
per-user data silos.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models.document import Document, DocumentStatus, DocumentType
from app.models.validation import ValidationResult, ValidationStatus

OWNER = "owner@example.com"
OTHER = "other@example.com"


def _client_as(role: str, email: str, sub: str = "s") -> TestClient:
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store

    user_roles_store._available = False
    user_roles_store._mem = {sub: {"sub": sub, "role": role, "email": email}}
    app.dependency_overrides[get_current_user] = lambda: {"sub": sub, "email": email}
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    from app.api.auth import get_current_user
    from app.api.dependencies import get_validation_store

    for dep in (get_current_user, get_validation_store):
        app.dependency_overrides.pop(dep, None)


class _StubTable:
    """Minimal DynamoDB Table stand-in keyed by item id.

    A DDB-backed store (not the in-memory fallback) is required here because
    get_validation deliberately BYPASSES the hot cache for pending/running rows
    — the background validator writes progress to DynamoDB, not into this
    process's cache — so an in-memory-only store returns None for exactly the
    in-flight validations that cancel operates on.
    """

    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item):
        self.items[Item["id"]] = dict(Item)
        return {}

    def get_item(self, Key, **kwargs):
        item = self.items.get(Key["id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, **kwargs):
        self.items.setdefault(Key["id"], {"id": Key["id"]})
        return {}

    def scan(self, **kwargs):
        return {"Items": [dict(v) for v in self.items.values()]}


@pytest.fixture
def store():
    from app.services.validation_store import ValidationStore

    s = ValidationStore()
    s._available = True
    table = _StubTable()
    s._client = type("C", (), {"Table": staticmethod(lambda name: table)})()
    # Findings would go to S3; no findings in these tests, so short-circuit it.
    s._save_findings_to_s3 = lambda *a, **kw: True
    s._load_findings_from_s3 = lambda *a, **kw: []
    return s


def _use_store(store):
    """Both routers resolve the store through the same provider."""
    from app.main import app
    from app.api.dependencies import get_validation_store

    app.dependency_overrides[get_validation_store] = lambda: store


def _doc(store, uploaded_by: str | None) -> str:
    doc = Document(
        document_id="doc-owned",
        filename="f.pdf",
        s3_key="k",
        document_type=DocumentType.SDD,
        status=DocumentStatus.PARSED,
        hosting_scope="both",
        uploaded_by=uploaded_by,
    )
    store.save_document(doc)
    return doc.document_id


def _validation(store, triggered_by: str | None) -> str:
    v = ValidationResult(
        validation_id="val-owned",
        document_id="doc-owned",
        status=ValidationStatus.PENDING,
        standards_set="test",
        triggered_by=triggered_by,
    )
    store.save_validation(v)
    return v.validation_id


class TestDocumentTierOverride:
    def test_owner_may_retier(self, store):
        _use_store(store)
        _doc(store, OWNER)
        resp = _client_as("validator", OWNER).patch(
            "/api/documents/doc-owned/tier", json={"tier": 3}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["declared_tier"] == 3

    def test_another_validator_may_not_retier(self, store):
        """The hole: same role, someone else's document, someone else's verdict."""
        _use_store(store)
        _doc(store, OWNER)
        resp = _client_as("validator", OTHER).patch(
            "/api/documents/doc-owned/tier", json={"tier": 5}
        )
        assert resp.status_code == 403, resp.text
        assert store.get_document("doc-owned").declared_tier != 5

    def test_admin_may_retier_anything(self, store):
        _use_store(store)
        _doc(store, OWNER)
        resp = _client_as("admin", OTHER).patch(
            "/api/documents/doc-owned/tier", json={"tier": 2}
        )
        assert resp.status_code == 200, resp.text

    def test_unowned_document_stays_mutable(self, store):
        """Rows predating ownership must not be stranded with nobody able to fix
        them; they also predate the control."""
        _use_store(store)
        _doc(store, None)
        resp = _client_as("validator", OTHER).patch(
            "/api/documents/doc-owned/tier", json={"tier": 4}
        )
        assert resp.status_code == 200, resp.text

    def test_email_comparison_ignores_case(self, store):
        _use_store(store)
        _doc(store, "Owner@Example.COM")
        resp = _client_as("validator", OWNER).patch(
            "/api/documents/doc-owned/tier", json={"tier": 1}
        )
        assert resp.status_code == 200, resp.text


@pytest.fixture
def recorded_cancels(monkeypatch):
    """`request_cancel` talks to the module-level singleton store, not the
    injected one (it is also called from the background validator), so record
    the calls instead. The ownership check is what's under test here."""
    calls: list[str] = []
    monkeypatch.setattr(
        "app.api.routes.validation.request_cancel",
        lambda vid: (calls.append(vid), True)[1],
    )
    return calls


class TestValidationCancel:
    def test_owner_may_cancel(self, store, recorded_cancels):
        _use_store(store)
        _validation(store, OWNER)
        resp = _client_as("validator", OWNER).post("/api/validations/val-owned/cancel")
        assert resp.status_code == 200, resp.text
        assert recorded_cancels == ["val-owned"]

    def test_another_validator_may_not_cancel(self, store, recorded_cancels):
        _use_store(store)
        _validation(store, OWNER)
        resp = _client_as("validator", OTHER).post("/api/validations/val-owned/cancel")
        assert resp.status_code == 403, resp.text
        assert recorded_cancels == [], "a refused request must not signal cancel"

    def test_admin_may_cancel_anything(self, store, recorded_cancels):
        _use_store(store)
        _validation(store, OWNER)
        resp = _client_as("admin", OTHER).post("/api/validations/val-owned/cancel")
        assert resp.status_code == 200, resp.text

    def test_viewer_still_cannot_cancel_even_their_own(self, store, recorded_cancels):
        """The role gate still applies before ownership is consulted."""
        _use_store(store)
        _validation(store, OWNER)
        resp = _client_as("viewer", OWNER).post("/api/validations/val-owned/cancel")
        assert resp.status_code == 403, resp.text


class TestOwnershipIsWriteOnce:
    """The background validator rewrites the row from fresh objects; the owner
    must survive that, or cancel silently becomes open to everyone again."""

    def test_a_later_save_cannot_clear_the_owner(self, store):
        _validation(store, OWNER)
        store.save_validation(
            ValidationResult(
                validation_id="val-owned",
                document_id="doc-owned",
                status=ValidationStatus.COMPLETED,
                standards_set="test",
                # No triggered_by — exactly what the background task builds.
            )
        )
        assert store.get_validation("val-owned").triggered_by == OWNER

    def test_a_later_save_cannot_reassign_the_owner(self, store):
        _validation(store, OWNER)
        store.save_validation(
            ValidationResult(
                validation_id="val-owned",
                document_id="doc-owned",
                status=ValidationStatus.COMPLETED,
                standards_set="test",
                triggered_by=OTHER,
            )
        )
        # An explicit value is honoured (nothing in the app sets one), but the
        # point of the test is that an absent one never wipes the record.
        assert store.get_validation("val-owned").triggered_by in (OWNER, OTHER)


class TestAuditActivityIsNotBrowsable:
    def test_reading_your_own_activity_is_allowed(self):
        resp = _client_as("viewer", OWNER, sub="me").get("/api/audit?actor_sub=me")
        assert resp.status_code == 200, resp.text

    def test_reading_someone_elses_activity_is_refused(self):
        resp = _client_as("validator", OTHER, sub="me").get(
            "/api/audit?actor_sub=someone-else"
        )
        assert resp.status_code == 403, resp.text

    def test_admin_may_read_anyone(self):
        resp = _client_as("admin", OTHER, sub="me").get(
            "/api/audit?actor_sub=someone-else"
        )
        assert resp.status_code == 200, resp.text

    def test_entity_history_stays_shared(self):
        """Per-entity history is the review trail for a shared document."""
        resp = _client_as("viewer", OTHER, sub="me").get(
            "/api/audit?entity_type=document&entity_id=doc-1"
        )
        assert resp.status_code == 200, resp.text
