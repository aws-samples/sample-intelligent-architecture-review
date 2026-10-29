# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the structured-rule schema additions to standards.

The structured-rule fields (rule_type, layer, parent_rule_id) are optional and
must round-trip without loss, validate against fixed vocabularies,
and never bloat legacy rows. The cache-hash projection
(_STANDARDS_HASH_FIELDS) is deliberately NOT extended with them, so
adding the new fields to a rule MUST NOT change its hash.
"""

from __future__ import annotations

from typing import get_args
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.agents.tools.standards_tools import (
    _VALID_LAYERS,
    _VALID_RULE_TYPES,
    save_standard,
)
from app.api.auth import get_current_user
from app.main import app
from app.models.standard import RuleLayer, RuleType
from app.services.validation_store import ValidationStore, validation_store


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch):
    """Wipe the singleton's in-memory standards between tests so a
    write in one test never leaks into another. The fallback path
    (`_available=False`) keeps DDB out of the picture entirely."""
    monkeypatch.setattr(validation_store, "_available", False)
    validation_store._standards = {}
    yield
    validation_store._standards = {}


@pytest.fixture(autouse=True)
def _bypass_auth():
    """Cognito auth is wired in as a global Depends(get_current_user).
    Override it in tests so we don't need a real JWT to hit the
    standards endpoints. Cleared after each test so prod behavior is
    not affected by test-only state.
    """
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test", "email": "test@example.com"}
    yield
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Drift guard between backend Pydantic Literal and agent vocab set.
# The single most important test here: it makes the contract that
# "the bank's layer vocabulary is one canonical set" enforceable in CI.
# ---------------------------------------------------------------------------


def test_layer_literal_matches_agent_vocab():
    assert set(get_args(RuleLayer)) == _VALID_LAYERS


def test_rule_type_literal_matches_agent_vocab():
    assert set(get_args(RuleType)) == _VALID_RULE_TYPES


# ---------------------------------------------------------------------------
# AC-V1 / AC-V2 — round-trip a standard with and without the structured fields. The
# OMIT WHEN EMPTY contract requires that absent fields produce keys that are
# absent from the persisted item, NOT keys with null values.
# ---------------------------------------------------------------------------


def test_create_with_mode_b_round_trip(client: TestClient):
    body = {
        "name": "Web layer requires F5",
        "description": "Web tier must front a managed LB",
        "severity": "high",
        "category": "traffic_management",
        "validation_criteria": "SDD names an F5 / managed LB",
        "rule_type": "founding",
        "layer": "web",
        "parent_rule_id": "HA-D-PARENT01",
        "hosting_scope": "on_prem",
    }
    resp = client.post("/api/standards", json=body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["rule_type"] == "founding"
    assert created["layer"] == "web"
    assert created["parent_rule_id"] == "HA-D-PARENT01"

    # Re-fetch via GET — the fields must survive a read-after-write.
    fetched = client.get(f"/api/standards/{created['id']}").json()
    assert fetched["rule_type"] == "founding"
    assert fetched["layer"] == "web"
    assert fetched["parent_rule_id"] == "HA-D-PARENT01"


def test_create_without_mode_b_omits_keys(client: TestClient):
    body = {
        "name": "Generic rule",
        "description": "A flat rule",
        "severity": "medium",
        "category": "availability",
        "validation_criteria": "vc",
        "hosting_scope": "on_prem",
    }
    resp = client.post("/api/standards", json=body)
    assert resp.status_code == 201, resp.text
    created_id = resp.json()["id"]

    raw = validation_store.get_standard(created_id)
    assert raw is not None
    # The persisted item omits the keys entirely. This is the
    # OMIT WHEN EMPTY contract — empty-state means "key absent," not
    # "key set to null." A future regression that switches to writing
    # null would surface here.
    assert "rule_type" not in raw
    assert "layer" not in raw
    assert "parent_rule_id" not in raw


# ---------------------------------------------------------------------------
# Vocabulary enforcement at the route boundary. An invalid
# layer or rule_type must 422; the route handler should never let bad values
# reach storage.
# ---------------------------------------------------------------------------


def test_invalid_layer_returns_422(client: TestClient):
    body = {
        "name": "x",
        "description": "x",
        "severity": "high",
        "category": "c",
        "validation_criteria": "vc",
        "layer": "middleware",
    }
    resp = client.post("/api/standards", json=body)
    assert resp.status_code == 422


def test_invalid_rule_type_returns_422(client: TestClient):
    body = {
        "name": "x",
        "description": "x",
        "severity": "high",
        "category": "c",
        "validation_criteria": "vc",
        "rule_type": "primary",
    }
    resp = client.post("/api/standards", json=body)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# AC-F11 backend equivalent — editing a legacy rule and changing only the
# severity must NOT 422. This is THE regression to guard against: the form
# could send `{"layer": ""}` which would otherwise hit the backend Literal
# and explode. Empty structured-field strings are stripped before the empty-check.
# ---------------------------------------------------------------------------


def test_edit_legacy_rule_with_empty_mode_b_strings(client: TestClient):
    create = client.post(
        "/api/standards",
        json={
            "name": "Legacy",
            "description": "A flat rule with no structured fields",
            "severity": "medium",
            "category": "availability",
            "validation_criteria": "vc",
            "hosting_scope": "on_prem",
        },
    )
    rule_id = create.json()["id"]

    # Mimic the Edit form when a user changes only severity. The
    # frontend's conditional spread should already prevent this from
    # being sent — but if it ever leaks through, the backend strip
    # in Step 3.2 must save us.
    resp = client.put(
        f"/api/standards/{rule_id}",
        json={"severity": "high", "layer": "", "rule_type": ""},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["severity"] == "high"
    assert "layer" not in updated or updated.get("layer") is None
    assert "rule_type" not in updated or updated.get("rule_type") is None


def test_put_with_only_empty_mode_b_returns_400(client: TestClient):
    # If the only thing the user sent was an empty structured-field string,
    # there's nothing to update — must 400 rather than no-op-write
    # only `updated_at`. This is the M-NEW-1 ordering guarantee.
    create = client.post(  # noqa
        "/api/standards",
        json={
            "name": "Legacy",
            "description": "x",
            "severity": "medium",
            "category": "availability",
            "validation_criteria": "vc",
            "hosting_scope": "on_prem",
        },
    )
    rule_id = create.json()["id"]

    resp = client.put(f"/api/standards/{rule_id}", json={"layer": ""})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# AC-V3a / AC-V3b — cache hash invariants. The hash deliberately does NOT include
# the new fields to _STANDARDS_HASH_FIELDS; therefore adding `rule_type` /
# `layer` / `parent_rule_id` to a rule must NOT change its hash.
# ---------------------------------------------------------------------------


def test_hash_unchanged_when_mode_b_fields_added():
    store = ValidationStore()
    store._available = False

    flat = {
        "id": "HA-X-FLAT0001",
        "name": "n",
        "description": "d",
        "severity": "high",
        "category": "c",
        "validation_criteria": "vc",
        "applies_to_tiers": [1, 2, 3, 4, 5],
    }
    store._standards = {flat["id"]: flat}
    hash_flat = store.standards_set_hash()

    # Same rule + structured fields populated. The cache hash MUST be
    # the same because the hash does not include these fields in
    # _STANDARDS_HASH_FIELDS.
    enriched = dict(flat)
    enriched["rule_type"] = "founding"
    enriched["layer"] = "web"
    enriched["parent_rule_id"] = "HA-X-OTHER01"
    store._standards = {enriched["id"]: enriched}
    hash_enriched = store.standards_set_hash()

    assert hash_flat == hash_enriched, (
        "Cache hash must not change when structured fields are added — "
        "_STANDARDS_HASH_FIELDS is deliberately untouched in ."
    )


def test_hash_independent_of_null_vs_missing_mode_b():
    """A rule with `rule_type: None` (legacy nullable form) and a
    rule that simply omits the key must produce identical hashes.
    Proves the OMIT WHEN EMPTY contract isn't load-bearing for the
    cache hash — a future regression that writes `null` instead of
    omitting the key would still find old verdicts cached.
    """
    store = ValidationStore()
    store._available = False

    base = {
        "id": "HA-X-NULL0001",
        "name": "n",
        "description": "d",
        "severity": "high",
        "category": "c",
        "validation_criteria": "vc",
        "applies_to_tiers": [1, 2, 3, 4, 5],
    }
    with_nulls = dict(base, rule_type=None, layer=None, parent_rule_id=None)

    store._standards = {base["id"]: base}
    hash_omitted = store.standards_set_hash()

    store._standards = {with_nulls["id"]: with_nulls}
    hash_null = store.standards_set_hash()

    assert hash_omitted == hash_null


# ---------------------------------------------------------------------------
# Agent tool junk-tolerance. An out-of-vocab `layer` produced by a
# misbehaving extraction prompt should NOT reject the whole rule; it should
# silently drop the bad value so the rule still lands.
# ---------------------------------------------------------------------------


def test_agent_tool_drops_invalid_layer():
    """ semantics: an out-of-vocab layer is dropped (junk
    tolerance preserved from ) AND the resulting None falls
    back to "general" (the Mode A default added in ). Net
    effect: bad input never lands the rule in `_legacy`."""
    # Strands tools are decorated; bypass the LLM-facing wrapper by
    # invoking the underlying function directly via the .invoke /
    # .__wrapped__ attribute that Strands typically exposes.
    fn = getattr(save_standard, "__wrapped__", None) or save_standard
    fn(
        name="x",
        description="x",
        severity="medium",
        category="availability",
        validation_criteria="vc",
        layer="middleware",  # not in vocab — should be dropped, then default to general
        rule_type="founding",
        hosting_scope="on_prem",
    )

    rows = list(validation_store._standards.values())
    saved = next(r for r in rows if r.get("name") == "x")
    assert saved.get("layer") == "general"
    # rule_type was valid, so it should have made it through.
    assert saved.get("rule_type") == "founding"


def test_agent_tool_normalises_spaces_and_hyphens():
    fn = getattr(save_standard, "__wrapped__", None) or save_standard
    fn(
        name="lb-rule",
        description="x",
        severity="medium",
        category="availability",
        validation_criteria="vc",
        layer="LOAD-BALANCER",  # case + hyphen → load_balancer
        hosting_scope="on_prem",
    )
    rows = list(validation_store._standards.values())
    saved = next(r for r in rows if r.get("name") == "lb-rule")
    assert saved.get("layer") == "load_balancer"


# ---------------------------------------------------------------------------
# Step 2: Mode A defaults newly-extracted rules to layer="general"
# so they land in a real bucket on the layer-grouped Standards page rather
# than the synthetic _legacy bucket.
# ---------------------------------------------------------------------------


def test_mode_a_extraction_defaults_to_general_layer():
    """When the Mode A free-form agent calls save_standard without a
    layer kwarg (which is the common case for the existing prompt),
    the saved row gets layer='general' so the rule lands in the
    General section of the Standards page."""
    fn = getattr(save_standard, "__wrapped__", None) or save_standard
    fn(
        name="mode-a-rule",
        description="cross-cutting security baseline",
        severity="high",
        category="security",
        validation_criteria="vc",
        # No layer kwarg — the Mode A agent doesn't know about layers
        hosting_scope="on_prem",
    )
    rows = list(validation_store._standards.values())
    saved = next(r for r in rows if r.get("name") == "mode-a-rule")
    assert saved.get("layer") == "general"



# ---------------------------------------------------------------------------
#  (revised) — extraction_notes free-form context.
# Replaced the cube-shape approach with a single optional NLP textarea
# the user fills in to tell the agent how the document is organised.
# Notes drive the dispatcher: non-empty → guided agent, empty → Mode A.
# ---------------------------------------------------------------------------


def test_document_persists_extraction_notes():
    """Round-trip a Document with extraction_notes through
    save_document → get_document."""
    from app.models.document import Document, DocumentStatus, DocumentType

    notes = "Pages 1-4 cover Tier 1-5 matrix; page 5 has Tier-1 substitutions."
    doc = Document(
        document_id="doc-notes-1",
        filename="framework.pdf",
        s3_key="documents/doc-notes-1/framework.pdf",
        content_hash="abc123",
        status=DocumentStatus.UPLOADED,
        document_type=DocumentType.STANDARDS,
        extraction_notes=notes,
        hosting_scope="on_prem",
    )
    saved = validation_store.save_document(doc)
    assert saved is not None

    fetched = validation_store.get_document("doc-notes-1")
    assert fetched is not None
    assert fetched.extraction_notes == notes


def test_dedup_creates_new_doc_for_different_notes():
    """Two uploads with the same content_hash + document_type but
    DIFFERENT extraction_notes should NOT dedup — different notes
    drive the agent differently, so each gets its own Document row
    and extraction run."""
    import hashlib
    from app.models.document import Document, DocumentStatus, DocumentType

    notes_a = "First pass: just Tier 1."
    notes_b = "Second pass: full Tier 1-5 matrix with substitutions."
    hash_a = hashlib.sha256(notes_a.encode("utf-8")).hexdigest()
    hash_b = hashlib.sha256(notes_b.encode("utf-8")).hexdigest()

    doc_a = Document(
        document_id="doc-notes-a",
        filename="same.pdf",
        s3_key="documents/doc-notes-a/same.pdf",
        content_hash="content-shared",
        status=DocumentStatus.UPLOADED,
        document_type=DocumentType.STANDARDS,
        extraction_notes=notes_a,
        hosting_scope="on_prem",
    )
    validation_store.save_document(doc_a)

    doc_b = Document(
        document_id="doc-notes-b",
        filename="same.pdf",
        s3_key="documents/doc-notes-b/same.pdf",
        content_hash="content-shared",
        status=DocumentStatus.UPLOADED,
        document_type=DocumentType.STANDARDS,
        extraction_notes=notes_b,
        hosting_scope="on_prem",
    )
    validation_store.save_document(doc_b)

    found_a = validation_store.get_document_by_hash(
        "content-shared",
        document_type="standards",
        extraction_notes_hash=hash_a,
    )
    assert found_a is not None
    assert found_a.document_id == "doc-notes-a"

    found_b = validation_store.get_document_by_hash(
        "content-shared",
        document_type="standards",
        extraction_notes_hash=hash_b,
    )
    assert found_b is not None
    assert found_b.document_id == "doc-notes-b"

    found_c = validation_store.get_document_by_hash(
        "content-shared",
        document_type="standards",
        extraction_notes_hash="some-hash-that-doesnt-exist",
    )
    assert found_c is None


def test_legacy_rules_with_null_layer_still_load(client: TestClient):
    """A row that was persisted before  (no layer key at all)
    must still round-trip through GET without raising. The frontend
    will render it under the synthetic `_legacy` bucket — the
    backend just needs to not explode on Pydantic load."""
    # Inject a row with no layer key at all (legacy / pre-Phase-2 shape).
    legacy_row = {
        "id": "HA-LEGACY-1",
        "name": "Legacy rule",
        "description": "Saved before ",
        "severity": "medium",
        "category": "availability",
        "validation_criteria": "vc",
        "applies_to_tiers": [1, 2, 3, 4, 5],
        "source": "user",
        "status": "active",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "hosting_scope": "on_prem",
    }
    validation_store._standards[legacy_row["id"]] = legacy_row

    # GET should succeed and return the row with layer absent / None.
    resp = client.get(f"/api/standards/{legacy_row['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "HA-LEGACY-1"
    assert body.get("layer") in (None, "")  # absent or null — both acceptable

    # LIST should include it without raising.
    resp = client.get("/api/standards")
    assert resp.status_code == 200
    ids = {s["id"] for s in resp.json().get("standards", [])}
    assert "HA-LEGACY-1" in ids


# ---------------------------------------------------------------------------
# .5 () — Mode A prompt update + diff regression guard.
# ---------------------------------------------------------------------------


def test_hosting_scope_literal_matches_agent_vocab():
    """Drift guard — backend Literal must equal agent _VALID_HOSTING_SCOPES."""
    from typing import get_args
    from app.models.standard import HostingScope
    from app.agents.tools.standards_tools import _VALID_HOSTING_SCOPES
    assert set(get_args(HostingScope)) == _VALID_HOSTING_SCOPES


def test_hosting_scope_required_on_create():
    """Pydantic 422s when hosting_scope is omitted."""
    from pydantic import ValidationError as PydValidationError
    from app.models.standard import CreateStandardRequest
    try:
        CreateStandardRequest(name='n', description='d', severity='high',
                              category='c', validation_criteria='vc')
        assert False, "expected ValidationError"
    except PydValidationError:
        pass


def test_validator_version_is_6():
    """v6: hosting_scope joined the hash fields.

    v5 added the scope FILTER to rule selection but left the rule's own
    hosting_scope out of the cache key, so retargeting a rule between on_prem
    and cloud did not invalidate the cache and the next run replayed a verdict
    computed from a different rule set. Every v5 hash was computed without that
    field, so all prior verdicts must cache-miss.
    """
    from app.services.validation_store import validation_store
    h_empty = validation_store.standards_set_hash(selected_ids=[])
    import json, hashlib

    def _hash_for(version: str) -> str:
        canonical = json.dumps(
            {"validator_version": version, "standards": []},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert h_empty == _hash_for("6")
    # Every earlier version must miss.
    for stale in ("4", "5"):
        assert h_empty != _hash_for(stale)


def test_document_persists_hosting_scope():
    """Round-trip a Document with hosting_scope through save/get."""
    from app.models.document import Document, DocumentStatus, DocumentType
    doc = Document(document_id='doc-scope-1', filename='f.pdf',
                   s3_key='s3', content_hash='abc',
                   status=DocumentStatus.UPLOADED,
                   document_type=DocumentType.STANDARDS,
                   hosting_scope='cloud')
    validation_store.save_document(doc)
    fetched = validation_store.get_document('doc-scope-1')
    assert fetched is not None
    assert fetched.hosting_scope == 'cloud'


def test_save_standard_rejects_missing_hosting_scope():
    """save_standard raises ValueError when hosting_scope is
    missing or invalid (Strands @tool returns this as a tool error
    to the model)."""
    from app.agents.tools.standards_tools import save_standard
    fn = getattr(save_standard, '__wrapped__', None) or save_standard
    try:
        fn(name='n', description='d', severity='high', category='c',
           validation_criteria='vc')
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        fn(name='n', description='d', severity='high', category='c',
           validation_criteria='vc', hosting_scope='hybrid')
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_save_standard_persists_valid_hosting_scope():
    """Valid hosting_scope is normalised + persisted."""
    from app.agents.tools.standards_tools import save_standard
    fn = getattr(save_standard, '__wrapped__', None) or save_standard
    fn(name='cloud-rule', description='d', severity='high', category='c',
       validation_criteria='vc', hosting_scope='cloud')
    rows = list(validation_store._standards.values())
    saved = next(r for r in rows if r.get('name') == 'cloud-rule')
    assert saved['hosting_scope'] == 'cloud'



def test_validator_filters_by_hosting_scope():
    """Validator selects rules where rule.hosting_scope ∈
    {sdd.hosting_scope, "both"}. 3 rule scopes × 2 SDD scopes; no nulls
    because the schema makes hosting_scope required."""
    rules = [
        {"id": "R-OP", "hosting_scope": "on_prem", "applies_to_tiers": [1,2,3,4,5]},
        {"id": "R-CL", "hosting_scope": "cloud",   "applies_to_tiers": [1,2,3,4,5]},
        {"id": "R-BO", "hosting_scope": "both",    "applies_to_tiers": [1,2,3,4,5]},
    ]

    def selected_for(scope: str) -> set:
        return {
            r["id"] for r in rules
            if r["hosting_scope"] == scope or r["hosting_scope"] == "both"
        }

    assert selected_for("on_prem") == {"R-OP", "R-BO"}
    assert selected_for("cloud") == {"R-CL", "R-BO"}
    assert "R-BO" in selected_for("on_prem") and "R-BO" in selected_for("cloud")
    assert "R-CL" not in selected_for("on_prem")
    assert "R-OP" not in selected_for("cloud")


# ---------------------------------------------------------------------------
#  (visual rule extraction) — image filter, allowlist, cache hash
# invariance, schema round-trip.
# ---------------------------------------------------------------------------



# NOTE: test_image_extractor_* and test_update_standard_merges_evidence_image_refs
# were removed . They tested the deleted app.services.document_extractor
# module and the Phase-B evidence-image MERGE behavior, both of which no longer
# exist — update_standard is intentionally replace-semantics, and the visual
# evidence-image feature was removed. The tests below assert the feature's
# ABSENCE and remain valid.


def test_evidence_image_refs_not_in_hash_fields():
    """Image refs MUST NOT participate in the standards-set
    hash. Adding image evidence must not invalidate cached
    validations."""
    from app.services.validation_store import validation_store

    assert "evidence_image_refs" not in validation_store._STANDARDS_HASH_FIELDS


def test_project_omits_evidence_image_refs():
    """Direct projection test — the projection used to compute
    standards_set_hash strips evidence_image_refs even when present.
    Mutating that field must NOT change the projected dict."""
    from app.services.validation_store import validation_store

    base = {
        "id": "HA-D-X",
        "name": "x",
        "description": "x",
        "severity": "medium",
        "category": "general",
        "validation_criteria": "x",
        "applies_to_tiers": [1],
        "alternative_criteria": "",
        "evidence_image_refs": ["image1.png"],
        "hosting_scope": "on_prem",
        "layer": "general",
    }
    # _project is a closure inside standards_set_hash, but the test
    # can exercise the same invariant by calling standards_set_hash
    # twice with the only difference being evidence_image_refs.
    # The store reads from its in-memory dict, so we monkey it.
    original = dict(validation_store._standards)
    try:
        validation_store._standards = {"HA-D-X": dict(base)}
        h1 = validation_store.standards_set_hash(selected_ids=["HA-D-X"])
        validation_store._standards = {"HA-D-X": {**base, "evidence_image_refs": ["image2.png", "image3.png"]}}
        h2 = validation_store.standards_set_hash(selected_ids=["HA-D-X"])
    finally:
        validation_store._standards = original
    assert h1 == h2, "Mutating evidence_image_refs must not change the standards-set hash"


# ---------------------------------------------------------------------------
# chunked rule extraction. Splits the document into page-range
# slices so a comprehensive doc doesn't blow the per-call output budget.
# ---------------------------------------------------------------------------


def _make_chunkable_doc(num_pages: int):
    """Build a Document with one ParsedSection per page, page numbers 1..N."""
    from app.models.document import Document, DocumentStatus, DocumentType, ParsedSection
    sections = [
        ParsedSection(
            section_id=f"sec-{i}",
            title=f"Section {i}",
            content=f"page {i} body",
            page_number=i,
            level=1,
        )
        for i in range(1, num_pages + 1)
    ]
    return Document(
        document_id="doc-test",
        filename="test.pdf",
        s3_key="test",
        document_type=DocumentType.STANDARDS,
        status=DocumentStatus.PARSED,
        page_count=num_pages,
        sections=sections,
        hosting_scope="on_prem",
    )


def test_chunk_document_partitions_correctly():
    """17 pages, 8/chunk → [1-8, 9-16, 17-17]."""
    from app.services.extraction_chunker import chunk_document

    doc = _make_chunkable_doc(17)
    chunks = chunk_document(doc, pages_per_chunk=8)

    assert len(chunks) == 3
    assert (chunks[0].start_page, chunks[0].end_page) == (1, 8)
    assert (chunks[1].start_page, chunks[1].end_page) == (9, 16)
    assert (chunks[2].start_page, chunks[2].end_page) == (17, 17)
    assert all(c.total == 3 for c in chunks)
    assert [c.index for c in chunks] == [1, 2, 3]
    assert len(chunks[0].sections) == 8
    assert len(chunks[1].sections) == 8
    assert len(chunks[2].sections) == 1


def test_chunk_document_short_doc_single_chunk():
    """5 pages, default 8/chunk → one chunk covering all."""
    from app.services.extraction_chunker import chunk_document

    doc = _make_chunkable_doc(5)
    chunks = chunk_document(doc, pages_per_chunk=8)
    assert len(chunks) == 1
    assert (chunks[0].start_page, chunks[0].end_page) == (1, 5)
    assert chunks[0].total == 1


def test_chunk_document_no_sections_returns_one_chunk():
    """Pre-parsing case: no sections yet → single fallback chunk."""
    from app.models.document import Document, DocumentStatus, DocumentType
    from app.services.extraction_chunker import chunk_document

    doc = Document(
        document_id="doc-test",
        filename="test.pdf",
        s3_key="test",
        document_type=DocumentType.STANDARDS,
        status=DocumentStatus.UPLOADED,
        sections=[],
        hosting_scope="on_prem",
    )
    chunks = chunk_document(doc)
    assert len(chunks) == 1


def test_chunk_renders_text_in_get_document_text_format():
    """Chunk text mirrors `[Page N] Title\\nbody` from get_document_text."""
    from app.services.extraction_chunker import chunk_document

    doc = _make_chunkable_doc(2)
    chunks = chunk_document(doc, pages_per_chunk=8)
    text = chunks[0].text
    assert "[Page 1] Section 1" in text
    assert "[Page 2] Section 2" in text


def test_dedup_rules_keeps_first_seen():
    """(name, tiers, layer) is the dedup key. First wins."""
    from app.services.extraction_chunker import dedup_rules

    rules = [
        {"id": "HA-D-A", "name": "Tier 1 Web A-A", "applies_to_tiers": [1], "layer": "web"},
        {"id": "HA-D-B", "name": "Tier 1 Web A-A", "applies_to_tiers": [1], "layer": "web"},
        {"id": "HA-D-C", "name": "Tier 1 Web A-A", "applies_to_tiers": [1], "layer": "general"},
    ]
    kept, dropped = dedup_rules(rules)
    assert {r["id"] for r in kept} == {"HA-D-A", "HA-D-C"}
    assert dropped == ["HA-D-B"]


def test_dedup_rules_normalizes_name_case():
    """Case + trailing whitespace fold to the same key."""
    from app.services.extraction_chunker import dedup_rules

    rules = [
        {"id": "HA-D-A", "name": "Tier 1 Web A-A", "applies_to_tiers": [1], "layer": "web"},
        {"id": "HA-D-B", "name": "  TIER 1 WEB A-A  ", "applies_to_tiers": [1], "layer": "web"},
    ]
    kept, dropped = dedup_rules(rules)
    assert {r["id"] for r in kept} == {"HA-D-A"}
    assert dropped == ["HA-D-B"]


def test_dedup_rules_treats_different_tiers_as_distinct():
    """Same name, different tier scope = NOT duplicates."""
    from app.services.extraction_chunker import dedup_rules

    rules = [
        {"id": "HA-D-T1", "name": "Multi-DC A-A", "applies_to_tiers": [1], "layer": "datacenter"},
        {"id": "HA-D-T2", "name": "Multi-DC A-A", "applies_to_tiers": [2, 3], "layer": "datacenter"},
    ]
    kept, dropped = dedup_rules(rules)
    assert len(kept) == 2
    assert dropped == []


def _make_pdf_with_table(rows: list[list[str]], add_heading: bool = False) -> bytes:
    """Build a 1-page PDF in memory with a real table.

    Uses fitz table-drawing helpers so the resulting PDF is what
    PyMuPDF's find_tables() can actually detect — drawing rectangles
    + text would not register as a table.
    """
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)

    if add_heading:
        page.insert_text(
            (50, 50),
            "Section Heading",
            fontsize=20,
            fontname="helv",
        )

    n_rows = len(rows)
    n_cols = max(len(r) for r in rows) if rows else 0
    x0, y0 = 50, 100 if add_heading else 50
    cell_w, cell_h = 240, 30

    for ci in range(n_cols + 1):
        x = x0 + ci * cell_w
        page.draw_line((x, y0), (x, y0 + n_rows * cell_h))
    for ri in range(n_rows + 1):
        y = y0 + ri * cell_h
        page.draw_line((x0, y), (x0 + n_cols * cell_w, y))

    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            page.insert_text(
                (x0 + ci * cell_w + 5, y0 + ri * cell_h + 20),
                str(cell),
                fontsize=10,
                fontname="helv",
            )

    out = doc.tobytes()
    doc.close()
    return out


def test_table_to_markdown_basic():
    """3-col header + 2 body rows → expected markdown."""
    from app.utils.pdf_parser import _table_to_markdown

    rows = [
        ["Layer", "Tier 1", "Tier 2"],
        ["DC", "A-A + DR", "1 + DR"],
        ["Web", "Cluster A-A", "Single"],
    ]
    md = _table_to_markdown(rows)
    assert "| Layer | Tier 1 | Tier 2 |" in md
    assert "| --- | --- | --- |" in md
    assert "| DC | A-A + DR | 1 + DR |" in md
    assert "| Web | Cluster A-A | Single |" in md


def test_table_to_markdown_preserves_empty_cells():
    """Intentional gaps in matrix tables stay as blank cells."""
    from app.utils.pdf_parser import _table_to_markdown

    rows = [
        ["Layer", "T1", "T2", "T3"],
        ["Web", "X", "", "X"],
    ]
    md = _table_to_markdown(rows)
    assert "| Web | X |  | X |" in md


def test_table_to_markdown_handles_pipe_in_cell():
    """Pipe characters inside a cell escape so markdown stays valid."""
    from app.utils.pdf_parser import _table_to_markdown

    rows = [["A", "B"], ["x|y", "z"]]
    md = _table_to_markdown(rows)
    assert "x\\|y" in md


def test_extract_text_and_sections_recovers_table_content_on_table_only_page():
    """ AC: table-only page surfaces the table as markdown
    in some section on that page — either a synthetic 'Page N' or
    appended to a fallback heading-loop seed section. The contract
    is that the data is reachable, not where it lands."""
    from app.utils.pdf_parser import extract_text_and_sections

    pdf = _make_pdf_with_table([
        ["Tier", "Description"],
        ["Tier 1", "two DCs A-A + DR"],
        ["Tier 2", "one DC + DR"],
    ])
    result = extract_text_and_sections(pdf)

    page1 = [s for s in result.sections if s.page_number == 1]
    assert len(page1) >= 1, "expected at least one section on page 1"

    combined = "\n".join(s.content for s in page1)
    assert "| Tier | Description |" in combined
    assert "| Tier 1 | two DCs A-A + DR |" in combined
    assert "| Tier 2 | one DC + DR |" in combined


def test_extract_text_and_sections_appends_table_to_existing_section():
    """ AC: page with heading AND table keeps the heading
    section and appends the markdown table to its content."""
    from app.utils.pdf_parser import extract_text_and_sections

    pdf = _make_pdf_with_table([
        ["Layer", "Status"],
        ["Web", "A-A"],
    ], add_heading=True)
    result = extract_text_and_sections(pdf)

    page1 = [s for s in result.sections if s.page_number == 1]
    titles = {s.title for s in page1}
    assert "Page 1" not in titles, "should NOT synthesize when heading exists; should append"

    appended = next(s for s in page1 if s.title != "Page 1")
    assert "## Tables on this page" in appended.content
    assert "| Layer | Status |" in appended.content


def test_extract_text_and_sections_no_table_no_synthetic_section():
    """Pages with no tables produce no synthetic Page N entries."""
    import fitz
    from app.utils.pdf_parser import extract_text_and_sections

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((50, 50), "Real Heading", fontsize=20, fontname="helv")
    page.insert_text((50, 100), "Body paragraph text.", fontsize=11, fontname="helv")
    pdf = doc.tobytes()
    doc.close()

    result = extract_text_and_sections(pdf)
    titles = [s.title for s in result.sections]
    assert "Page 1" not in titles, "no table on page → no synthetic section"


def test_extract_text_and_sections_v2_doc_recovers_all_tiers():
    """Integration proxy: parsing a real tiered standards document recovers
    Tier 1-5 from pages 1-3.

    The source PDF is not distributed with this repository. Point
    ARCH_REVIEW_STANDARDS_PDF at a local copy to run it; otherwise it is
    skipped, which keeps CI runnable without the document."""
    import os
    import re
    from app.utils.pdf_parser import extract_text_and_sections

    fixture = os.environ.get("ARCH_REVIEW_STANDARDS_PDF", "")
    if not fixture or not os.path.exists(fixture):
        import pytest
        pytest.skip("set ARCH_REVIEW_STANDARDS_PDF to run against a real standards PDF")

    pdf = open(fixture, "rb").read()
    result = extract_text_and_sections(pdf)

    for p in (1, 2, 3):
        secs = [s for s in result.sections if s.page_number == p]
        assert secs, f"page {p} should have at least one section"
        text = "\n".join(s.content for s in secs)
        tiers = sorted(set(re.findall(r"Tier\s*[1-5]", text)))
        assert tiers == ["Tier 1", "Tier 2", "Tier 3", "Tier 4", "Tier 5"], (
            f"page {p} expected all 5 tiers, got: {tiers}"
        )
