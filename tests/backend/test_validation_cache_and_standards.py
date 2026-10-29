# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The validation cache key, cache lookup, and standards CRUD.

The cache key is the highest-stakes pure function in the store: if it fails to
change when a rule changes, a document is served a stale verdict computed
against a rulebook that no longer exists. If it changes when nothing meaningful
changed, every validation re-runs and re-bills Bedrock.

The S3 findings helpers are covered here too — findings live in S3 precisely
because a finding set can exceed DynamoDB's item limit, so a silent failure
there loses a completed validation's entire output.
"""

from __future__ import annotations

import json

import pytest

from app.models.validation import (
    ComplianceStatus,
    Finding,
    Severity,
    ValidationResult,
    ValidationStatus,
)
from app.services.validation_store import ValidationStore


def _standard(sid: str, **over) -> dict:
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
    }
    base.update(over)
    return base


@pytest.fixture
def store():
    """In-memory store — the hash and filter logic is storage-independent."""
    s = ValidationStore()
    s._available = False
    return s


# ---------------------------------------------------------------------------
# standards_set_hash — the cache key
# ---------------------------------------------------------------------------

class TestStandardsSetHash:
    def test_is_stable_for_identical_input(self, store):
        store._standards = {"a": _standard("a")}
        assert store.standards_set_hash() == store.standards_set_hash()

    def test_is_independent_of_insertion_order(self, store):
        """Write order must not invalidate caches; the projection sorts by id."""
        store._standards = {"a": _standard("a"), "b": _standard("b")}
        first = store.standards_set_hash()
        store._standards = {"b": _standard("b"), "a": _standard("a")}
        assert store.standards_set_hash() == first

    @pytest.mark.parametrize(
        "field,new_value",
        [
            ("validation_criteria", "something else entirely"),
            ("severity", "low"),
            ("description", "reworded"),
            ("hosting_scope", "cloud"),
            ("applies_to_tiers", [1]),
        ],
    )
    def test_changes_when_a_verdict_relevant_field_changes(self, store, field, new_value):
        store._standards = {"a": _standard("a")}
        before = store.standards_set_hash()
        store._standards = {"a": _standard("a", **{field: new_value})}
        assert store.standards_set_hash() != before, (
            f"editing {field} must invalidate cached verdicts"
        )

    def test_changes_when_a_rule_is_added(self, store):
        store._standards = {"a": _standard("a")}
        before = store.standards_set_hash()
        store._standards["b"] = _standard("b")
        assert store.standards_set_hash() != before

    def test_changes_when_a_rule_is_removed(self, store):
        store._standards = {"a": _standard("a"), "b": _standard("b")}
        before = store.standards_set_hash()
        del store._standards["b"]
        assert store.standards_set_hash() != before

    def test_ignores_metadata_churn(self, store):
        """Timestamps and provenance are excluded on purpose — re-saving a rule
        unchanged must not force every validation to re-run."""
        store._standards = {"a": _standard("a", created_at="2026-01-01")}
        before = store.standards_set_hash()
        store._standards = {
            "a": _standard("a", created_at="2026-07-28", source_document="doc-9")
        }
        assert store.standards_set_hash() == before

    def test_proposed_rules_do_not_affect_the_hash(self, store):
        """list_standards defaults to active, so a pending proposal must not
        invalidate caches before anyone approves it."""
        store._standards = {"a": _standard("a")}
        before = store.standards_set_hash()
        store._standards["p"] = _standard("p", status="proposed")
        assert store.standards_set_hash() == before

    def test_selected_ids_narrow_the_hash(self, store):
        store._standards = {"a": _standard("a"), "b": _standard("b")}
        assert store.standards_set_hash(selected_ids=["a"]) != store.standards_set_hash()

    def test_selection_order_does_not_matter(self, store):
        store._standards = {"a": _standard("a"), "b": _standard("b")}
        assert store.standards_set_hash(selected_ids=["a", "b"]) == (
            store.standards_set_hash(selected_ids=["b", "a"])
        )

    def test_transient_rules_are_folded_in(self, store):
        """A one-off rule included in a run changes what was checked, so it must
        change the key."""
        store._standards = {"a": _standard("a")}
        before = store.standards_set_hash()
        after = store.standards_set_hash(transient_standards=[_standard("t")])
        assert after != before

    def test_legacy_rows_without_tiers_hash_as_all_tiers(self, store):
        """Defaulting differently would make every legacy rule cache-miss."""
        explicit = _standard("a", applies_to_tiers=[1, 2, 3, 4, 5])
        legacy = _standard("a")
        legacy.pop("applies_to_tiers")
        store._standards = {"a": explicit}
        with_explicit = store.standards_set_hash()
        store._standards = {"a": legacy}
        assert store.standards_set_hash() == with_explicit

    def test_unsortable_tiers_fall_back_to_all_tiers(self, store):
        store._standards = {"a": _standard("a", applies_to_tiers=["not", "ints"])}
        legacy = _standard("a")
        legacy.pop("applies_to_tiers")
        first = store.standards_set_hash()
        store._standards = {"a": legacy}
        assert store.standards_set_hash() == first

    def test_tier_order_does_not_change_the_hash(self, store):
        store._standards = {"a": _standard("a", applies_to_tiers=[3, 1, 2])}
        first = store.standards_set_hash()
        store._standards = {"a": _standard("a", applies_to_tiers=[1, 2, 3])}
        assert store.standards_set_hash() == first

    def test_the_validator_version_is_part_of_the_key(self, store):
        """Bumping the version must invalidate everything: an old verdict answered
        a different question."""
        store._standards = {"a": _standard("a")}
        real = store.standards_set_hash()
        # Recompute the same projection with a different envelope version.
        standards = [
            {k: (sorted(v) if k == "applies_to_tiers" else v)
             for k, v in ((f, _standard("a").get(f, "")) for f in store._STANDARDS_HASH_FIELDS)}
        ]
        import hashlib

        forged = hashlib.sha256(
            json.dumps(
                {"validator_version": "999", "standards": standards},
                sort_keys=True, separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert real != forged

    def test_an_empty_library_still_hashes(self, store):
        store._standards = {}
        assert isinstance(store.standards_set_hash(), str)
        assert len(store.standards_set_hash()) == 64


# ---------------------------------------------------------------------------
# find_cached_validation
# ---------------------------------------------------------------------------

def _validation(vid, status, doc="doc-1", dh="dh", sh="sh") -> ValidationResult:
    return ValidationResult(
        validation_id=vid,
        document_id=doc,
        status=status,
        standards_set="s",
        document_content_hash=dh,
        standards_set_hash=sh,
    )


class TestFindCachedValidation:
    def test_finds_a_completed_match(self, store):
        store.save_validation(_validation("val-1", ValidationStatus.COMPLETED))
        got = store.find_cached_validation("doc-1", "dh", "sh")
        assert got is not None and got.validation_id == "val-1"

    def test_finds_an_in_flight_pending_match(self, store):
        """Returning the pending run is what stops two identical validations
        racing and double-billing Bedrock."""
        store.save_validation(_validation("val-1", ValidationStatus.PENDING))
        assert store.find_cached_validation("doc-1", "dh", "sh") is not None

    def test_a_different_document_is_not_a_match(self, store):
        store.save_validation(_validation("val-1", ValidationStatus.COMPLETED))
        assert store.find_cached_validation("doc-OTHER", "dh", "sh") is None

    def test_changed_document_content_misses(self, store):
        store.save_validation(_validation("val-1", ValidationStatus.COMPLETED))
        assert store.find_cached_validation("doc-1", "dh-CHANGED", "sh") is None

    def test_changed_standards_set_misses(self, store):
        """This is the whole point of the rules hash — an edited rulebook must
        not serve the old verdict."""
        store.save_validation(_validation("val-1", ValidationStatus.COMPLETED))
        assert store.find_cached_validation("doc-1", "dh", "sh-CHANGED") is None

    @pytest.mark.parametrize(
        "status", [ValidationStatus.FAILED, ValidationStatus.CANCELLED]
    )
    def test_terminal_non_success_runs_are_not_reused(self, store, status):
        store.save_validation(_validation("val-1", status))
        assert store.find_cached_validation("doc-1", "dh", "sh") is None

    def test_no_history_returns_none(self, store):
        assert store.find_cached_validation("doc-1", "dh", "sh") is None


class TestIsoToEpoch:
    def test_parses_an_iso_timestamp(self):
        assert ValidationStore._iso_to_epoch("2026-07-28T12:00:00") > 0

    def test_tolerates_a_trailing_z(self):
        assert ValidationStore._iso_to_epoch("2026-07-28T12:00:00Z") > 0

    @pytest.mark.parametrize("bad", [None, "", "not a date", 12345])
    def test_unparseable_values_sort_last_instead_of_raising(self, bad):
        """This feeds a sort key; raising here would break cache lookup entirely
        because one malformed row poisons the whole comparison."""
        assert ValidationStore._iso_to_epoch(bad) == 0.0


# ---------------------------------------------------------------------------
# standards CRUD
# ---------------------------------------------------------------------------

class TestStandardsCrud:
    def test_create_then_get(self, store):
        store.create_standard(_standard("a"))
        assert store.get_standard("a")["name"] == "Rule a"

    def test_get_unknown_returns_none(self, store):
        assert store.get_standard("nope") is None

    def test_update_replaces_the_given_fields(self, store):
        store.create_standard(_standard("a"))
        out = store.update_standard("a", {"severity": "low"})
        assert out["severity"] == "low"
        assert out["name"] == "Rule a"  # untouched fields survive

    def test_update_unknown_returns_none(self, store):
        assert store.update_standard("nope", {"severity": "low"}) is None

    def test_delete_removes_the_row(self, store):
        store.create_standard(_standard("a"))
        assert store.delete_standard("a") is True
        assert store.get_standard("a") is None

    def test_delete_unknown_reports_false(self, store):
        assert store.delete_standard("nope") is False

    def test_structured_rule_fields_round_trip(self, store):
        """rule_type / layer / parent_rule_id are pass-through; dropping them
        would flatten the decision tree."""
        store.create_standard(
            _standard("sub", rule_type="substitution", layer="storage",
                      parent_rule_id="founding-1")
        )
        got = store.get_standard("sub")
        assert got["rule_type"] == "substitution"
        assert got["layer"] == "storage"
        assert got["parent_rule_id"] == "founding-1"


class TestListStandardsFiltering:
    def test_defaults_to_active_only(self, store):
        store._standards = {
            "a": _standard("a"),
            "p": _standard("p", status="proposed"),
            "z": _standard("z", status="archived"),
        }
        assert {s["id"] for s in store.list_standards()} == {"a"}

    def test_rows_without_a_status_count_as_active(self, store):
        """A missing column must never silently archive an existing rule."""
        legacy = _standard("legacy")
        legacy.pop("status")
        store._standards = {"legacy": legacy}
        assert {s["id"] for s in store.list_standards()} == {"legacy"}

    def test_a_null_status_counts_as_active(self, store):
        store._standards = {"n": _standard("n", status=None)}
        assert {s["id"] for s in store.list_standards()} == {"n"}

    def test_include_proposed_returns_everything(self, store):
        store._standards = {
            "a": _standard("a"),
            "p": _standard("p", status="proposed"),
            "z": _standard("z", status="archived"),
        }
        assert len(store.list_standards(include_proposed=True)) == 3

    def test_status_none_disables_filtering(self, store):
        store._standards = {"a": _standard("a"), "p": _standard("p", status="proposed")}
        assert len(store.list_standards(status=None)) == 2

    def test_an_explicit_status_selects_that_status(self, store):
        store._standards = {"a": _standard("a"), "p": _standard("p", status="proposed")}
        assert {s["id"] for s in store.list_standards(status="proposed")} == {"p"}


# ---------------------------------------------------------------------------
# S3 findings storage
# ---------------------------------------------------------------------------

class _S3:
    def __init__(self, fail_get=None, fail_put=None):
        self.objects: dict[str, bytes] = {}
        self.fail_get = fail_get
        self.fail_put = fail_put

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        if self.fail_put:
            raise self.fail_put
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket=None, Key=None):
        if self.fail_get:
            raise self.fail_get
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        class _Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": _Body(self.objects[Key])}


@pytest.fixture
def s3_store(store):
    """A store whose document-store S3 client is a recorder.

    `doc_store` is a property backed by `_doc_store`, so the backing attribute
    is what has to be replaced.
    """
    s3 = _S3()
    store._doc_store = type("DS", (), {"client": s3})()
    store._s3 = s3
    return store


def _finding(fid="f1") -> Finding:
    return Finding(
        finding_id=fid,
        requirement_id="r1",
        requirement_title="Multi-AZ",
        severity=Severity.CRITICAL,
        status=ComplianceStatus.FAIL,
        evidence="single AZ only",
    )


class TestFindingsS3RoundTrip:
    def test_findings_round_trip_through_s3(self, s3_store):
        assert s3_store._save_findings_to_s3("val-1", [_finding()]) is True
        loaded = s3_store._load_findings_from_s3("val-1")
        assert len(loaded) == 1
        assert loaded[0]["requirement_title"] == "Multi-AZ"

    def test_the_key_is_namespaced_by_validation(self, s3_store):
        s3_store._save_findings_to_s3("val-9", [_finding()])
        assert "validations/val-9/findings.json" in s3_store._s3.objects

    def test_a_missing_object_yields_an_empty_list(self, s3_store):
        """A validation that produced no findings has no object; that is not an
        error."""
        assert s3_store._load_findings_from_s3("never-written") == []

    def test_a_put_failure_reports_false(self, s3_store):
        s3_store._s3.fail_put = RuntimeError("S3 down")
        assert s3_store._save_findings_to_s3("val-1", [_finding()]) is False

    def test_a_get_failure_yields_an_empty_list(self, s3_store):
        s3_store._s3.fail_get = RuntimeError("S3 down")
        assert s3_store._load_findings_from_s3("val-1") == []

    def test_no_s3_client_is_a_no_op(self, store):
        store._doc_store = type("DS", (), {"client": None})()
        assert store._save_findings_to_s3("val-1", [_finding()]) is False
        assert store._load_findings_from_s3("val-1") == []
