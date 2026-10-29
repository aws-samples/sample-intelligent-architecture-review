# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every mutating endpoint is role-gated.

A review found eleven write endpoints carrying only the router-level auth
dependency, so any authenticated user — including a viewer — could call them.
The worst was POST /api/references: a reference body is injected into every
matching rule's validation prompt, so an unprivileged writer could steer a
compliance verdict. This suite pins the gates shut.

The point of the last test here is that it is a SWEEP, not a list: a new
mutating route added without a role dependency fails it automatically, rather
than waiting for the next audit.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class _FakeTable:
    """Minimal in-memory stand-in for a boto3 Table.

    references_store has no in-memory fallback (unlike the other stores), so
    swap the table itself rather than flipping a flag.
    """

    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item):  # noqa: N803 — boto3's kwarg name
        self.items[Item["id"]] = Item
        return {}

    def get_item(self, Key):  # noqa: N803
        found = self.items.get(Key["id"])
        return {"Item": found} if found else {}

    def delete_item(self, Key):  # noqa: N803
        self.items.pop(Key["id"], None)
        return {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


@pytest.fixture(autouse=True)
def _stores_in_memory(monkeypatch):
    """Keep every store off DynamoDB for these tests."""
    from app.services.references_store import references_store

    monkeypatch.setattr(references_store, "_table", _FakeTable())
    yield


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


_REFERENCE_BODY = {
    "name": "probe",
    "body": "injected context",
    "applies_to_tiers": [1],
    "hosting_scope": "both",
}


class TestReferences:
    """References feed validation prompts, so writes are curator+."""

    def test_viewer_cannot_create(self):
        r = _client_as("viewer").post("/api/references", json=_REFERENCE_BODY)
        assert r.status_code == 403, r.text

    def test_validator_cannot_create(self):
        """Running validations doesn't imply authoring the context they use."""
        r = _client_as("validator").post("/api/references", json=_REFERENCE_BODY)
        assert r.status_code == 403, r.text

    def test_curator_can_create(self):
        r = _client_as("curator").post("/api/references", json=_REFERENCE_BODY)
        assert r.status_code == 201, r.text

    def test_viewer_cannot_update(self):
        created = _client_as("curator").post("/api/references", json=_REFERENCE_BODY).json()
        r = _client_as("viewer").put(
            f"/api/references/{created['id']}", json={"body": "tampered"}
        )
        assert r.status_code == 403, r.text

    def test_viewer_cannot_delete(self):
        created = _client_as("curator").post("/api/references", json=_REFERENCE_BODY).json()
        r = _client_as("viewer").delete(f"/api/references/{created['id']}")
        assert r.status_code == 403, r.text

    def test_viewer_can_still_read(self):
        """Read access is deliberately open to any authenticated caller."""
        assert _client_as("viewer").get("/api/references").status_code == 200

    def test_deleting_a_missing_reference_is_404_not_500(self):
        r = _client_as("curator").delete("/api/references/HA-R-DOESNOTEXIST")
        assert r.status_code == 404, r.text


class TestOtherWrites:
    def test_viewer_cannot_override_a_document_tier(self):
        """declared_tier decides which rules a document is judged against."""
        r = _client_as("viewer").patch("/api/documents/doc-1/tier", json={"tier": 5})
        assert r.status_code == 403, r.text

    def test_viewer_cannot_send_a_teams_notification(self):
        r = _client_as("viewer").post("/api/teams/notify", json={
            "title": "probe", "message": "probe",
        })
        assert r.status_code == 403, r.text

    def test_viewer_cannot_drive_the_curator(self):
        r = _client_as("viewer").post("/api/standards/curate",
                                      json={"document_id": "doc-1"})
        assert r.status_code == 403, r.text


# Mutating routes that deliberately carry no require_role dependency, each with
# the reason it is safe. Anything NOT listed here must be gated.
_ALLOWED_UNGATED = {
    # Gated inside the handler instead: the required role depends on
    # document_type (standards -> curator, SDD -> validator), which a single
    # route-level dependency can't express. Covered by test_documents_upload_*.
    ("POST", "/api/documents"): "in-handler role split by document_type",
    # Read-shaped: POST only because the question goes in the body. Answers
    # questions about documents the caller can already read.
    ("POST", "/api/queries"): "read-only Q&A, no state change",
    ("POST", "/api/queries/stream"): "read-only Q&A, no state change",
    # Inbound integration endpoint: it replies to a Teams payload rather than
    # mutating app state. Still behind the router-level auth dependency.
    ("POST", "/api/teams/webhook"): "inbound integration, no state change",
}


def test_no_mutating_route_is_left_ungated():
    """Sweep every POST/PUT/PATCH/DELETE route and assert it resolves a role.

    Catches the next endpoint added without a gate. A route counts as gated when
    a role dependency appears anywhere in its dependency chain; the handful that
    gate differently are listed in _ALLOWED_UNGATED with a reason.
    """
    from app.main import app
    from app.api.rbac import require_role

    # The closure require_role() returns; identify it by its qualname.
    gate_marker = require_role.__name__

    ungated: list[str] = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        if not (methods & {"POST", "PUT", "PATCH", "DELETE"}):
            continue
        path = getattr(route, "path", "")
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue

        # Walk the dependency tree looking for a require_role closure.
        found = False
        stack = [dependant]
        while stack:
            dep = stack.pop()
            call = getattr(dep, "call", None)
            if call is not None:
                qual = getattr(call, "__qualname__", "")
                if gate_marker in qual:
                    found = True
                    break
            stack.extend(getattr(dep, "dependencies", []) or [])
        if found:
            continue
        for method in sorted(methods & {"POST", "PUT", "PATCH", "DELETE"}):
            if (method, path) not in _ALLOWED_UNGATED:
                ungated.append(f"{method} {path}")

    assert not ungated, (
        "mutating route(s) with no role gate — add "
        "Depends(require_role(...)), or add an entry to _ALLOWED_UNGATED "
        "explaining why it is safe:\n  " + "\n  ".join(sorted(ungated))
    )
