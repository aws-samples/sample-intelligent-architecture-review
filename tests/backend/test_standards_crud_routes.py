# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Standards library CRUD and the proposal-approval state machine.

Every rule here becomes an input to future compliance verdicts, so the
invariants are about what lands in the library:

  * OMIT WHEN EMPTY — a blank <select> serialises to "", and persisting that as
    a layer/rule_type would put an empty-string sentinel in the cache-hash
    projection and break the decision tree's bucket matching.
  * The in-flight guard (409) — approving a proposal while its extraction run is
    still deduping means approving a row that may be about to be merged away.
  * approve() is a three-way state machine (add / edit / delete) where the wrong
    branch silently edits or archives the wrong rule.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class _Store:
    def __init__(self, standards=None):
        self.standards = dict(standards or {})
        self.created: list[dict] = []
        self.deleted: list[str] = []

    def get_standard(self, sid):
        return self.standards.get(sid)

    def create_standard(self, standard):
        self.created.append(standard)
        self.standards[standard["id"]] = standard
        return standard

    def update_standard(self, sid, updates):
        if sid not in self.standards:
            return None
        self.standards[sid].update(updates)
        return self.standards[sid]

    def delete_standard(self, sid):
        self.deleted.append(sid)
        return self.standards.pop(sid, None) is not None

    def list_standards(self, **kw):
        return list(self.standards.values())

    def get_document(self, document_id):
        return None

    def get_extraction_state(self, document_id):
        return None


def _standard(sid="HA-1", **over) -> dict:
    base = {
        "id": sid,
        "name": f"Rule {sid}",
        "description": "d",
        "severity": "high",
        "category": "availability",
        "validation_criteria": "c",
        "hosting_scope": "both",
        "status": "active",
        "applies_to_tiers": [1, 2, 3, 4, 5],
        "source": "user",
    }
    base.update(over)
    return base


@pytest.fixture
def client(monkeypatch):
    """Curator-authenticated client with the module-level store replaced."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store
    import app.api.routes.standards as mod

    def _make(standards=None, role="curator", in_flight=False):
        store = _Store(standards)
        monkeypatch.setattr(mod, "validation_store", store)
        monkeypatch.setattr(mod, "_extraction_in_flight_for", lambda rule: in_flight)

        user_roles_store._available = False
        user_roles_store._mem = {"s": {"sub": "s", "role": role, "email": "c@x.com"}}
        app.dependency_overrides[get_current_user] = lambda: {
            "sub": "s", "email": "c@x.com"
        }
        return TestClient(app), store

    yield _make

    app.dependency_overrides.pop(get_current_user, None)


def _create_body(**over) -> dict:
    base = {
        "name": "Multi-AZ deployment",
        "description": "Services span two AZs.",
        "severity": "critical",
        "category": "availability",
        "validation_criteria": "Confirm two AZs are named.",
        "hosting_scope": "both",
    }
    base.update(over)
    return base


class TestCreateStandard:
    def test_creates_with_a_generated_id(self, client):
        c, store = client()
        resp = c.post("/api/standards", json=_create_body())
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"].startswith("HA-U-")
        assert body["name"] == "Multi-AZ deployment"
        assert body["source"] == "user"

    def test_records_the_creator_for_the_audit_trail(self, client):
        c, _ = client()
        assert c.post("/api/standards", json=_create_body()).json()["created_by"] == (
            "c@x.com"
        )

    def test_stamps_both_timestamps(self, client):
        c, _ = client()
        body = c.post("/api/standards", json=_create_body()).json()
        assert body["created_at"] and body["updated_at"]

    def test_structured_rule_fields_are_persisted_when_given(self, client):
        c, _ = client()
        body = c.post(
            "/api/standards",
            json=_create_body(rule_type="substitution", layer="data",
                              parent_rule_id="HA-1"),
        ).json()
        assert body["rule_type"] == "substitution"
        assert body["layer"] == "data"
        assert body["parent_rule_id"] == "HA-1"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_structured_fields_are_omitted_entirely(self, client, blank):
        """A blank <select> serialises to ""; storing that would put an
        empty-string sentinel in the cache-hash projection and break the
        decision tree's (tier, layer) bucket matching."""
        c, _ = client()
        body = c.post(
            "/api/standards",
            json=_create_body(rule_type=blank, layer=blank, parent_rule_id=blank),
        ).json()
        for key in ("rule_type", "layer", "parent_rule_id"):
            assert key not in body, f"{key} should be absent, not empty"

    def test_omitted_structured_fields_stay_absent(self, client):
        c, _ = client()
        body = c.post("/api/standards", json=_create_body()).json()
        assert "rule_type" not in body
        assert "layer" not in body

    def test_a_validator_may_not_create(self, client):
        c, _ = client(role="validator")
        assert c.post("/api/standards", json=_create_body()).status_code == 403

    def test_a_viewer_may_not_create(self, client):
        c, _ = client(role="viewer")
        assert c.post("/api/standards", json=_create_body()).status_code == 403

    def test_an_admin_may_create(self, client):
        c, _ = client(role="admin")
        assert c.post("/api/standards", json=_create_body()).status_code == 201

    def test_a_missing_required_field_is_422(self, client):
        c, _ = client()
        body = _create_body()
        del body["validation_criteria"]
        assert c.post("/api/standards", json=body).status_code == 422


class TestUpdateStandard:
    def test_updates_the_given_fields(self, client):
        c, store = client({"HA-1": _standard()})
        resp = c.put("/api/standards/HA-1", json={"severity": "low"})
        assert resp.status_code == 200, resp.text
        assert store.standards["HA-1"]["severity"] == "low"

    def test_unspecified_fields_are_untouched(self, client):
        c, store = client({"HA-1": _standard()})
        c.put("/api/standards/HA-1", json={"severity": "low"})
        assert store.standards["HA-1"]["name"] == "Rule HA-1"

    def test_unknown_standard_is_404(self, client):
        c, _ = client()
        assert c.put("/api/standards/nope", json={"severity": "low"}).status_code == 404

    def test_an_in_flight_rule_is_409(self, client):
        """Editing a proposal mid-extraction races the dedup that may merge it
        away."""
        c, _ = client({"HA-1": _standard(status="proposed")}, in_flight=True)
        resp = c.put("/api/standards/HA-1", json={"severity": "low"})
        assert resp.status_code == 409
        assert "still extracting" in resp.json()["detail"]

    def test_a_validator_may_not_update(self, client):
        c, _ = client({"HA-1": _standard()}, role="validator")
        assert c.put("/api/standards/HA-1", json={"severity": "low"}).status_code == 403


class TestDeleteStandard:
    def test_deletes_the_rule(self, client):
        c, store = client({"HA-1": _standard()})
        resp = c.delete("/api/standards/HA-1")
        assert resp.status_code in (200, 204), resp.text
        assert "HA-1" in store.deleted

    def test_unknown_standard_is_404(self, client):
        c, _ = client()
        assert c.delete("/api/standards/nope").status_code == 404

    def test_a_validator_may_not_delete(self, client):
        c, _ = client({"HA-1": _standard()}, role="validator")
        assert c.delete("/api/standards/HA-1").status_code == 403


class TestApproveProposal:
    def test_an_add_proposal_becomes_active(self, client):
        c, store = client({"P-1": _standard("P-1", status="proposed",
                                            proposal_kind="add")})
        resp = c.post("/api/standards/P-1/approve")
        assert resp.status_code == 200, resp.text
        assert store.standards["P-1"]["status"] == "active"

    def test_approval_is_attributed(self, client):
        """Who approved a rule is the audit question that matters most."""
        c, store = client({"P-1": _standard("P-1", status="proposed",
                                            proposal_kind="add")})
        c.post("/api/standards/P-1/approve")
        assert store.standards["P-1"]["approved_by"] == "c@x.com"
        assert store.standards["P-1"]["approved_at"]

    def test_a_proposal_without_a_kind_defaults_to_add(self, client):
        c, store = client({"P-1": _standard("P-1", status="proposed")})
        assert c.post("/api/standards/P-1/approve").status_code == 200
        assert store.standards["P-1"]["status"] == "active"

    def test_approving_an_already_active_add_is_a_no_op(self, client):
        c, _ = client({"P-1": _standard("P-1", status="active",
                                        proposal_kind="add")})
        assert c.post("/api/standards/P-1/approve").status_code == 200

    def test_an_edit_proposal_copies_onto_the_target(self, client):
        c, store = client({
            "HA-1": _standard("HA-1", name="Old name"),
            "P-1": _standard("P-1", status="proposed", proposal_kind="edit",
                             target_id="HA-1", name="New name"),
        })
        resp = c.post("/api/standards/P-1/approve")
        assert resp.status_code == 200, resp.text
        assert store.standards["HA-1"]["name"] == "New name"

    def test_an_applied_edit_proposal_is_removed(self, client):
        """Leaving it behind would let a second approve re-apply a stale edit."""
        c, store = client({
            "HA-1": _standard("HA-1"),
            "P-1": _standard("P-1", status="proposed", proposal_kind="edit",
                             target_id="HA-1", name="New name"),
        })
        c.post("/api/standards/P-1/approve")
        assert "P-1" in store.deleted

    def test_a_delete_proposal_archives_the_target(self, client):
        """Soft delete: an archived rule stops applying but stays auditable."""
        c, store = client({
            "HA-1": _standard("HA-1"),
            "P-1": _standard("P-1", status="proposed", proposal_kind="delete",
                             target_id="HA-1"),
        })
        resp = c.post("/api/standards/P-1/approve")
        assert resp.status_code == 200, resp.text
        assert store.standards["HA-1"]["status"] == "archived"

    def test_an_edit_whose_target_vanished_is_a_conflict(self, client):
        """409, not 404: the proposal exists, but the rule it would patch was
        deleted since the proposal was raised. Applying it blindly would
        resurrect a deleted rule."""
        c, _ = client({
            "P-1": _standard("P-1", status="proposed", proposal_kind="edit",
                             target_id="does-not-exist"),
        })
        resp = c.post("/api/standards/P-1/approve")
        assert resp.status_code == 409
        assert "no longer exists" in resp.json()["detail"]

    def test_an_edit_without_a_target_id_is_a_400(self, client):
        c, _ = client({
            "P-1": _standard("P-1", status="proposed", proposal_kind="edit"),
        })
        resp = c.post("/api/standards/P-1/approve")
        assert resp.status_code == 400
        assert "no target_id" in resp.json()["detail"]

    def test_unknown_proposal_is_404(self, client):
        c, _ = client()
        assert c.post("/api/standards/nope/approve").status_code == 404

    def test_an_in_flight_extraction_blocks_approval(self, client):
        """The review gate's accuracy guarantee: approving before dedup and
        cross-reference wiring finish means approving a row that may be merged
        away moments later."""
        c, _ = client({"P-1": _standard("P-1", status="proposed")}, in_flight=True)
        resp = c.post("/api/standards/P-1/approve")
        assert resp.status_code == 409

    def test_a_validator_may_not_approve(self, client):
        c, _ = client({"P-1": _standard("P-1", status="proposed")}, role="validator")
        assert c.post("/api/standards/P-1/approve").status_code == 403


class TestRejectProposal:
    def test_rejecting_removes_the_proposal(self, client):
        c, store = client({"P-1": _standard("P-1", status="proposed")})
        resp = c.post("/api/standards/P-1/reject")
        assert resp.status_code == 200, resp.text
        assert "P-1" in store.deleted

    def test_unknown_proposal_is_404(self, client):
        c, _ = client()
        assert c.post("/api/standards/nope/reject").status_code == 404

    def test_an_in_flight_extraction_blocks_rejection(self, client):
        c, _ = client({"P-1": _standard("P-1", status="proposed")}, in_flight=True)
        assert c.post("/api/standards/P-1/reject").status_code == 409

    def test_a_viewer_may_not_reject(self, client):
        c, _ = client({"P-1": _standard("P-1", status="proposed")}, role="viewer")
        assert c.post("/api/standards/P-1/reject").status_code == 403


class TestGuardNotInFlight:
    def test_raises_409_when_in_flight(self, monkeypatch):
        from fastapi import HTTPException
        import app.api.routes.standards as mod

        monkeypatch.setattr(mod, "_extraction_in_flight_for", lambda rule: True)
        with pytest.raises(HTTPException) as exc:
            mod._guard_not_in_flight({"id": "P-1"})
        assert exc.value.status_code == 409

    def test_passes_when_settled(self, monkeypatch):
        import app.api.routes.standards as mod

        monkeypatch.setattr(mod, "_extraction_in_flight_for", lambda rule: False)
        mod._guard_not_in_flight({"id": "P-1"})  # must not raise
