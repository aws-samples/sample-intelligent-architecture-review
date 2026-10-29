# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Route tests for the extraction review-batch endpoints (the review gate).

Exercises GET /extraction-batches, GET /extraction-batches/{id}, and the
bulk approve-all/reject-all, plus the source filter that keeps document
extraction proposals out of the Curate proposals panel. Uses the real
in-memory validation_store (_available=False) so no DynamoDB is needed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _seed(store, *, rid, batch, source="document", status="proposed",
          needs_review=False, doc="rulebook.pdf (doc-1)", kind="add"):
    row = {
        "id": rid,
        "name": rid,
        "description": "d",
        "severity": "high",
        "category": "failover",
        "validation_criteria": "v",
        "applies_to_tiers": [1],
        "layer": "web",
        "hosting_scope": "both",
        "source": source,
        "source_document": doc,
        "status": status,
        "created_at": "2026-07-24T00:00:00Z",
        "updated_at": "2026-07-24T00:00:00Z",
        "proposal_kind": kind,
    }
    if batch:
        row["extraction_batch_id"] = batch
    if needs_review:
        row["needs_review"] = True
    store._standards[rid] = row
    return row


@pytest.fixture
def harness():
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.validation_store import validation_store

    app.dependency_overrides[get_current_user] = lambda: {"sub": "test-user"}
    validation_store._available = False
    validation_store._standards = {}
    try:
        yield validation_store
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        validation_store._standards = {}


def _client():
    from app.main import app
    return TestClient(app)


def test_list_batches_groups_and_counts(harness):
    _seed(harness, rid="HA-D-A1", batch="ext-b1", needs_review=True)
    _seed(harness, rid="HA-D-A2", batch="ext-b1")
    _seed(harness, rid="HA-D-A3", batch="ext-b2", doc="other.pdf (doc-2)")

    resp = _client().get("/api/standards/extraction-batches")
    assert resp.status_code == 200
    batches = {b["extraction_batch_id"]: b for b in resp.json()["batches"]}
    assert batches["ext-b1"]["total"] == 2
    assert batches["ext-b1"]["needs_review"] == 1
    assert batches["ext-b2"]["total"] == 1


def test_batch_summary_flags_in_flight_while_extracting(harness):
    """A batch whose run is still extracting is reported in_flight/finalizing so
    the UI can hold back its provisional (pre-dedup) count. When the job is
    terminal it settles to pending."""
    from unittest.mock import patch
    _seed(harness, rid="HA-D-A1", batch="ext-b1", doc="rulebook.pdf (doc-1)")

    with patch(
        "app.services.extraction_log.get_job",
        return_value={"status": "extracting"},
    ):
        b = _client().get("/api/standards/extraction-batches").json()["batches"][0]
        assert b["in_flight"] is True
        assert b["status"] == "finalizing"

    with patch(
        "app.services.extraction_log.get_job",
        return_value={"status": "completed"},
    ):
        b = _client().get("/api/standards/extraction-batches").json()["batches"][0]
        assert b["in_flight"] is False
        assert b["status"] == "pending"


def test_list_batches_filters_by_document(harness):
    _seed(harness, rid="HA-D-A1", batch="ext-b1", doc="rulebook.pdf (doc-1)")
    _seed(harness, rid="HA-D-A3", batch="ext-b2", doc="other.pdf (doc-2)")

    resp = _client().get("/api/standards/extraction-batches", params={"document_id": "doc-1"})
    batches = resp.json()["batches"]
    assert len(batches) == 1
    assert batches[0]["extraction_batch_id"] == "ext-b1"


def test_get_batch_returns_rules_needs_review_first(harness):
    _seed(harness, rid="HA-D-OK", batch="ext-b1", needs_review=False)
    _seed(harness, rid="HA-D-FLAG", batch="ext-b1", needs_review=True)

    resp = _client().get("/api/standards/extraction-batches/ext-b1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["rules"][0]["id"] == "HA-D-FLAG"  # needs_review sorted first


def test_get_unknown_batch_404(harness):
    assert _client().get("/api/standards/extraction-batches/ext-nope").status_code == 404


def test_approve_blocked_while_extraction_in_flight(harness):
    """Race guard: approving before extraction+synthesis finishes must 409,
    so a rule can't be pulled out of the proposed set before cross-reference
    wiring + dedup run over the whole batch."""
    from unittest.mock import patch
    _seed(harness, rid="HA-D-A1", batch="ext-b1", doc="rulebook.pdf (doc-1)")
    with patch(
        "app.services.extraction_log.get_job",
        return_value={"status": "extracting"},
    ):
        one = _client().post("/api/standards/HA-D-A1/approve")
        assert one.status_code == 409
        batch = _client().post("/api/standards/extraction-batches/ext-b1/approve-all")
        assert batch.status_code == 409
    # Rule untouched.
    assert harness._standards["HA-D-A1"]["status"] == "proposed"


def test_approve_allowed_when_job_terminal(harness):
    from unittest.mock import patch
    _seed(harness, rid="HA-D-A1", batch="ext-b1", doc="rulebook.pdf (doc-1)")
    with patch(
        "app.services.extraction_log.get_job",
        return_value={"status": "completed"},
    ):
        resp = _client().post("/api/standards/HA-D-A1/approve")
        assert resp.status_code in (200, 409) and resp.status_code == 200
    assert harness._standards["HA-D-A1"]["status"] == "active"


def test_approve_all_flips_batch_active(harness):
    _seed(harness, rid="HA-D-A1", batch="ext-b1")
    _seed(harness, rid="HA-D-A2", batch="ext-b1")

    resp = _client().post("/api/standards/extraction-batches/ext-b1/approve-all")
    assert resp.status_code == 200
    assert resp.json()["approved"] == 2
    # Both now active → no longer in the proposed batch view.
    assert harness._standards["HA-D-A1"]["status"] == "active"
    assert harness._standards["HA-D-A2"]["status"] == "active"
    assert _client().get("/api/standards/extraction-batches/ext-b1").status_code == 404


def test_reject_all_deletes_batch(harness):
    _seed(harness, rid="HA-D-A1", batch="ext-b1")
    _seed(harness, rid="HA-D-A2", batch="ext-b1")

    resp = _client().post("/api/standards/extraction-batches/ext-b1/reject-all")
    assert resp.status_code == 200
    assert resp.json()["rejected"] == 2
    assert "HA-D-A1" not in harness._standards
    assert "HA-D-A2" not in harness._standards


def test_batches_exclude_curator_proposals(harness):
    # A curator-chat proposal (source=generated) must NOT appear as a batch.
    _seed(harness, rid="HA-G-1", batch=None, source="generated")
    _seed(harness, rid="HA-D-A1", batch="ext-b1", source="document")

    resp = _client().get("/api/standards/extraction-batches")
    ids = [b["extraction_batch_id"] for b in resp.json()["batches"]]
    assert ids == ["ext-b1"]


def test_source_filter_keeps_curate_panel_clean(harness):
    # GET /api/standards?status=proposed&source=generated returns ONLY curator
    # proposals, so extraction batches don't bleed into the Curate panel.
    _seed(harness, rid="HA-G-1", batch=None, source="generated")
    _seed(harness, rid="HA-D-A1", batch="ext-b1", source="document")

    resp = _client().get("/api/standards", params={"status": "proposed", "source": "generated"})
    rows = resp.json()["standards"]
    assert [r["id"] for r in rows] == ["HA-G-1"]


def test_validation_active_list_excludes_proposed(harness):
    # The security invariant: proposed rules never appear in the active list
    # that validation reads.
    _seed(harness, rid="HA-D-A1", batch="ext-b1", status="proposed")
    _seed(harness, rid="HA-D-ACT", batch=None, status="active")

    resp = _client().get("/api/standards")  # default active-only
    ids = [r["id"] for r in resp.json()["standards"]]
    assert ids == ["HA-D-ACT"]


# ---- scope-mismatch surfacing + batch set-scope ----

def test_batch_summary_surfaces_a_scope_mismatch(harness):
    """A rule stamped with a content_scope verdict that contradicts its
    hosting_scope must surface on the batch summary so the review panel can
    warn — otherwise a wrong upload-time scope is invisible at approval."""
    r = _seed(harness, rid="HA-D-A1", batch="ext-b1")
    r["hosting_scope"] = "on_prem"
    r["content_scope"] = {"declared": "on_prem", "inferred": "cloud",
                          "confidence": "high", "rationale": "AWS regions, managed RDS"}
    resp = _client().get("/api/standards/extraction-batches")
    b = {x["extraction_batch_id"]: x for x in resp.json()["batches"]}["ext-b1"]
    assert b["scope_mismatch"] is not None
    assert b["scope_mismatch"]["inferred"] == "cloud"
    assert b["hosting_scope"] == "on_prem"


def test_batch_summary_no_mismatch_when_scope_agrees(harness):
    """Falsifies the surfacing test: a verdict whose inferred scope equals the
    declared scope must NOT warn, or every batch would show a false alarm."""
    r = _seed(harness, rid="HA-D-A1", batch="ext-b1")
    r["hosting_scope"] = "cloud"
    r["content_scope"] = {"declared": "cloud", "inferred": "cloud",
                          "confidence": "high", "rationale": "matches"}
    resp = _client().get("/api/standards/extraction-batches")
    b = {x["extraction_batch_id"]: x for x in resp.json()["batches"]}["ext-b1"]
    assert b["scope_mismatch"] is None


def test_set_scope_retargets_every_rule_in_the_batch(harness):
    _seed(harness, rid="HA-D-A1", batch="ext-b1")
    _seed(harness, rid="HA-D-A2", batch="ext-b1")
    resp = _client().post("/api/standards/extraction-batches/ext-b1/set-scope",
                          json={"hosting_scope": "cloud"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 2
    assert harness._standards["HA-D-A1"]["hosting_scope"] == "cloud"
    assert harness._standards["HA-D-A2"]["hosting_scope"] == "cloud"
    # Rules stay proposed — retarget is not approval.
    assert harness._standards["HA-D-A1"]["status"] == "proposed"


def test_set_scope_clears_the_mismatch_warning(harness):
    """Aligning declared scope to the inferred one must clear the warning, so
    the fix is visibly complete on the panel."""
    r = _seed(harness, rid="HA-D-A1", batch="ext-b1")
    r["hosting_scope"] = "on_prem"
    r["content_scope"] = {"declared": "on_prem", "inferred": "cloud",
                          "confidence": "high", "rationale": "cloud"}
    _client().post("/api/standards/extraction-batches/ext-b1/set-scope",
                   json={"hosting_scope": "cloud"})
    resp = _client().get("/api/standards/extraction-batches")
    b = {x["extraction_batch_id"]: x for x in resp.json()["batches"]}["ext-b1"]
    assert b["scope_mismatch"] is None  # inferred(cloud) now == hosting_scope(cloud)


def test_set_scope_rejects_a_bad_value(harness):
    _seed(harness, rid="HA-D-A1", batch="ext-b1")
    resp = _client().post("/api/standards/extraction-batches/ext-b1/set-scope",
                          json={"hosting_scope": "mars"})
    assert resp.status_code == 422


def test_set_scope_on_unknown_batch_is_404(harness):
    resp = _client().post("/api/standards/extraction-batches/nope/set-scope",
                          json={"hosting_scope": "cloud"})
    assert resp.status_code == 404


def test_set_scope_blocked_while_extraction_in_flight(harness):
    from unittest.mock import patch
    _seed(harness, rid="HA-D-A1", batch="ext-b1", doc="rulebook.pdf (doc-1)")
    with patch("app.services.extraction_log.get_job",
               return_value={"status": "extracting"}):
        resp = _client().post("/api/standards/extraction-batches/ext-b1/set-scope",
                              json={"hosting_scope": "cloud"})
        assert resp.status_code == 409
    assert harness._standards["HA-D-A1"]["hosting_scope"] == "both"  # untouched


# ---- extraction start-guard: no second run while one is in flight ----

class _Doc:
    """Minimal document stand-in with the attributes /extract reads."""
    def __init__(self, doc_id):
        self.document_id = doc_id
        self.filename = "rulebook.pdf"
        self._extraction_state = None


def test_extract_409s_when_a_run_is_already_in_flight(harness):
    """A standards upload auto-triggers extraction; a manual /extract on top of
    that would mint a SECOND batch. The guard returns 409 instead."""
    from unittest.mock import patch
    harness._documents = {"doc-1": _Doc("doc-1")}
    with patch("app.services.extraction_log.is_in_flight", return_value=True):
        resp = _client().post("/api/standards/extract?document_id=doc-1")
    assert resp.status_code == 409
    assert "already running" in resp.json()["detail"].lower()
    harness._documents = {}


def test_extract_starts_when_no_run_is_in_flight(harness):
    """The guard must not block a legitimate (re-)extraction of an idle doc."""
    from unittest.mock import patch
    harness._documents = {"doc-2": _Doc("doc-2")}
    # is_in_flight False AND stub the background task so no real extraction runs.
    with patch("app.services.extraction_log.is_in_flight", return_value=False), \
         patch("app.api.routes.standards._extract_in_background", return_value=None):
        resp = _client().post("/api/standards/extract?document_id=doc-2")
    assert resp.status_code == 200
    assert resp.json()["status"] == "starting"
    harness._documents = {}


def test_extract_404s_for_an_unknown_document(harness):
    resp = _client().post("/api/standards/extract?document_id=nope")
    assert resp.status_code == 404
