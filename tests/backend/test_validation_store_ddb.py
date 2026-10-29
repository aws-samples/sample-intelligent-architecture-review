# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DynamoDB round-trip in validation_store.

Existing suites drive this store through its in-memory fallback
(`_available = False`), which skips every serialization path — that is why the
module sat at 54% while being the most-used service in the app. These tests run
it with `_available = True` against a stub table, so the real put_item /
get_item / scan code executes without AWS.

What matters here:
  * Documents are stored SLIM (no section content) to stay under DDB's 400KB
    item limit, so the metadata reconstruction on read has to be lossless for
    the fields it does keep.
  * Upload dedup keys on content_hash plus a hash of the user's extraction
    notes; getting that wrong either creates duplicate documents or returns a
    stale extraction for changed notes.
  * The in-memory cache holds FULL content and must win over the slim DDB row.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

import pytest

from app.models.document import (
    Document,
    DocumentStatus,
    DocumentType,
    ParsedSection,
    SlideStatus,
)
from app.services.validation_store import ValidationStore


class _StubTable:
    """In-process stand-in for a boto3 Table, keyed by item id."""

    def __init__(self, name="t"):
        self.name = name
        self.items: dict[str, dict] = {}
        self.scan_calls: list[dict] = []
        self.fail_with: Exception | None = None

    def put_item(self, Item):
        if self.fail_with:
            raise self.fail_with
        self.items[Item["id"]] = dict(Item)
        return {}

    def get_item(self, Key, **kw):
        if self.fail_with:
            raise self.fail_with
        item = self.items.get(Key["id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, **kw):
        if self.fail_with:
            raise self.fail_with
        self.items.setdefault(Key["id"], {"id": Key["id"]})
        return {}

    def scan(self, **kw):
        if self.fail_with:
            raise self.fail_with
        self.scan_calls.append(kw)
        items = [dict(v) for v in self.items.values()]
        expr = kw.get("FilterExpression")
        if isinstance(expr, str):
            # list_documents passes a string expression + attribute values,
            # e.g. "document_type = :dtype".
            values = kw.get("ExpressionAttributeValues", {})
            attr, _, placeholder = expr.partition(" = ")
            expected = values.get(placeholder.strip())
            items = [i for i in items if i.get(attr.strip()) == expected]
        elif expr is not None:
            items = [i for i in items if _matches(expr, i)]
        return {"Items": items}


def _matches(expr, item) -> bool:
    """Evaluate a boto3 Attr condition against a plain dict.

    Only the operators this store uses (eq, AND) need to work.
    """
    fmt = getattr(expr, "expression_format", None)
    if fmt is None:
        return True
    operator = expr.expression_operator
    values = expr._values
    if operator == "AND":
        return all(_matches(v, item) for v in values)
    if operator == "=":
        attr, expected = values
        return item.get(attr.name) == expected
    return True


@pytest.fixture
def store():
    """A store wired to stub tables, exercising the DynamoDB code path."""
    s = ValidationStore()
    s._available = True
    tables: dict[str, _StubTable] = {}

    def _table(name):
        return tables.setdefault(name, _StubTable(name))

    s._client = type("C", (), {"Table": staticmethod(_table)})()
    s._tables = tables  # test handle
    s._save_findings_to_s3 = lambda *a, **kw: True
    s._load_findings_from_s3 = lambda *a, **kw: []
    return s


def _doc(**over) -> Document:
    base = dict(
        document_id="doc-1",
        filename="sdd.pdf",
        s3_key="documents/doc-1/sdd.pdf",
        document_type=DocumentType.SDD,
        status=DocumentStatus.PARSED,
        hosting_scope="both",
        content_hash="hash-abc",
        file_size_bytes=2048,
        page_count=12,
    )
    base.update(over)
    return Document(**base)


def _section(page=1, content="body") -> ParsedSection:
    return ParsedSection(
        section_id=f"s{page}", title=f"Section {page}", content=content, page_number=page
    )


class TestSaveDocument:
    def test_writes_the_row_and_returns_the_document(self, store):
        out = store.save_document(_doc())
        assert out.document_id == "doc-1"
        # The table name comes from settings, so read back whichever was written.
        written = next(t for t in store._tables.values() if t.items)
        assert written.items["doc-1"]["filename"] == "sdd.pdf"
        assert written.items["doc-1"]["s3_key"].endswith("sdd.pdf")

    def test_stores_counts_not_section_content(self, store):
        """Sections can be megabytes; DDB caps an item at 400KB. Only the counts
        go on the row, with the content living in memory and S3."""
        store.save_document(_doc(sections=[_section(1, "x" * 5000), _section(2)]))
        item = next(t for t in store._tables.values() if t.items).items["doc-1"]
        assert item["section_count"] == 2
        assert "x" * 5000 not in str(item)
        assert "sections" not in item

    def test_optional_fields_are_omitted_when_unset(self, store):
        """Older rows must not gain null keys, and absence is what the read path
        treats as "not set"."""
        store.save_document(_doc(content_hash=None))
        item = next(t for t in store._tables.values() if t.items).items["doc-1"]
        # source_format is excluded: it has a non-None model default ("pdf"),
        # so the row always carries it.
        for key in (
            "content_hash", "declared_tier", "tier_source", "extraction_notes",
            "slide_count", "slides_json", "uploaded_by", "original_s3_key",
        ):
            assert key not in item, f"{key} should be omitted when unset"

    def test_tier_metadata_is_persisted_when_set(self, store):
        store.save_document(_doc(declared_tier=2, tier_source="user"))
        item = next(t for t in store._tables.values() if t.items).items["doc-1"]
        assert item["declared_tier"] == 2
        assert item["tier_source"] == "user"

    def test_pptx_fields_are_persisted_and_json_encoded(self, store):
        """slides[] is JSON-encoded to avoid DDB's nested-list coercion."""
        slides = [
            SlideStatus(
                slide_index=1, slide_type="text_heavy", rule_count=3,
                status="extracted",
            ),
            SlideStatus(
                slide_index=2, slide_type="diagram_heavy", rule_count=0,
                status="failed", error="boom",
            ),
        ]
        store.save_document(
            _doc(source_format="pptx", slide_count=2, slides=slides,
                 original_s3_key="documents/doc-1/orig.pptx")
        )
        item = next(t for t in store._tables.values() if t.items).items["doc-1"]
        assert item["source_format"] == "pptx"
        assert item["slide_count"] == 2
        assert item["original_s3_key"].endswith(".pptx")
        assert '"rule_count": 3' in item["slides_json"]
        assert '"error": "boom"' in item["slides_json"]

    def test_a_ddb_failure_still_returns_the_document(self, store):
        """Upload must not fail because the metadata write failed — the bytes
        are already in S3 and the in-memory copy drives processing."""
        from botocore.exceptions import ClientError

        failing = _StubTable()
        failing.fail_with = ClientError({"Error": {"Code": "Throttling"}}, "PutItem")
        store._client = type("C", (), {"Table": staticmethod(lambda n: failing)})()
        assert store.save_document(_doc()).document_id == "doc-1"

    def test_the_in_memory_copy_is_updated_too(self, store):
        doc = _doc(sections=[_section()])
        store.save_document(doc)
        assert store._documents["doc-1"] is doc


class TestGetDocument:
    def test_the_cache_wins_over_the_slim_row(self, store):
        """The cached object carries sections; the DDB row does not. Preferring
        the row would silently lose parsed content mid-pipeline."""
        doc = _doc(sections=[_section(1, "full text here")])
        store.save_document(doc)
        got = store.get_document("doc-1")
        assert got.sections and got.sections[0].content == "full text here"

    def test_reconstructs_from_the_row_when_not_cached(self, store):
        store.save_document(_doc(declared_tier=3, tier_source="auto"))
        store._documents.clear()
        got = store.get_document("doc-1")
        assert got is not None
        assert got.filename == "sdd.pdf"
        assert got.declared_tier == 3
        assert got.tier_source == "auto"
        assert got.sections == []  # slim row carries no content

    def test_unknown_id_returns_none(self, store):
        assert store.get_document("nope") is None

    def test_uploaded_by_survives_the_round_trip(self, store):
        """Ownership authorizes tier changes; losing it on read would open that
        mutation to everyone."""
        store.save_document(_doc(uploaded_by="owner@example.com"))
        store._documents.clear()
        assert store.get_document("doc-1").uploaded_by == "owner@example.com"


class TestGetDocumentByHash:
    def _notes_hash(self, notes: str) -> str:
        return hashlib.sha256(notes.strip().encode()).hexdigest()

    def test_finds_a_prior_upload_of_identical_bytes(self, store):
        store.save_document(_doc())
        store._documents.clear()
        assert store.get_document_by_hash("hash-abc").document_id == "doc-1"

    def test_a_different_hash_does_not_match(self, store):
        store.save_document(_doc())
        store._documents.clear()
        assert store.get_document_by_hash("hash-other") is None

    def test_filters_by_document_type(self, store):
        """An SDD and a standards document can share bytes; dedup must not
        collapse them into one row."""
        store.save_document(_doc())
        store._documents.clear()
        assert store.get_document_by_hash("hash-abc", document_type="standards") is None
        assert store.get_document_by_hash("hash-abc", document_type="sdd") is not None

    def test_same_bytes_with_different_notes_is_not_a_match(self, store):
        """Re-uploading a PDF with new extraction notes must produce a NEW
        document and a fresh extraction, not the cached one."""
        store.save_document(_doc(extraction_notes="focus on storage"))
        store._documents.clear()
        assert store.get_document_by_hash(
            "hash-abc", extraction_notes_hash=self._notes_hash("something else")
        ) is None

    def test_same_bytes_and_same_notes_is_a_match(self, store):
        store.save_document(_doc(extraction_notes="focus on storage"))
        store._documents.clear()
        got = store.get_document_by_hash(
            "hash-abc", extraction_notes_hash=self._notes_hash("focus on storage")
        )
        assert got is not None and got.document_id == "doc-1"

    def test_omitting_the_notes_hash_ignores_that_dimension(self, store):
        store.save_document(_doc(extraction_notes="anything"))
        store._documents.clear()
        assert store.get_document_by_hash("hash-abc") is not None

    def test_the_cache_is_checked_before_ddb(self, store):
        doc = _doc(sections=[_section()])
        store.save_document(doc)
        table = next(t for t in store._tables.values() if t.items)
        table.scan_calls.clear()
        assert store.get_document_by_hash("hash-abc") is doc
        assert table.scan_calls == []

    def test_the_oldest_match_wins(self, store):
        first = _doc(document_id="doc-old", uploaded_at=datetime(2026, 1, 1))
        second = _doc(document_id="doc-new", uploaded_at=datetime(2026, 6, 1))
        store.save_document(first)
        store.save_document(second)
        store._documents.clear()
        assert store.get_document_by_hash("hash-abc").document_id == "doc-old"

    def test_a_scan_failure_returns_none(self, store):
        from botocore.exceptions import ClientError

        failing = _StubTable()
        failing.fail_with = ClientError({"Error": {"Code": "Throttling"}}, "Scan")
        store._client = type("C", (), {"Table": staticmethod(lambda n: failing)})()
        assert store.get_document_by_hash("hash-abc") is None


class TestListDocuments:
    def test_returns_every_row(self, store):
        store.save_document(_doc(document_id="a", content_hash="h1"))
        store.save_document(_doc(document_id="b", content_hash="h2"))
        store._documents.clear()
        assert {d.document_id for d in store.list_documents()} == {"a", "b"}

    def test_filters_by_document_type(self, store):
        store.save_document(_doc(document_id="sdd-1"))
        store.save_document(
            _doc(document_id="std-1", document_type=DocumentType.STANDARDS)
        )
        store._documents.clear()
        ids = {d.document_id for d in store.list_documents(document_type="standards")}
        assert ids == {"std-1"}

    def test_empty_table_yields_empty_list(self, store):
        assert store.list_documents() == []


class TestUpdateDocumentStatus:
    def test_sets_the_status(self, store):
        store.save_document(_doc(status=DocumentStatus.UPLOADED))
        out = store.update_document_status("doc-1", DocumentStatus.PARSING)
        assert out.status == DocumentStatus.PARSING

    def test_records_an_error_message(self, store):
        store.save_document(_doc())
        out = store.update_document_status(
            "doc-1", DocumentStatus.FAILED, error_message="parse blew up"
        )
        assert out.status == DocumentStatus.FAILED
        assert out.error_message == "parse blew up"

    def test_parsed_stamps_parsed_at(self, store):
        store.save_document(_doc(status=DocumentStatus.PARSING))
        out = store.update_document_status("doc-1", DocumentStatus.PARSED)
        assert out.parsed_at is not None

    def test_unknown_document_returns_none(self, store):
        assert store.update_document_status("nope", DocumentStatus.PARSED) is None


class TestFindingsInS3:
    def test_findings_are_written_to_s3_not_the_row(self, store):
        """A validation's findings can exceed the DDB item limit on their own."""
        from app.models.validation import (
            ComplianceStatus, Finding, Severity, ValidationResult, ValidationStatus,
        )

        written: list[tuple] = []
        store._save_findings_to_s3 = lambda vid, f: written.append((vid, len(f))) or True

        store.save_validation(
            ValidationResult(
                validation_id="val-1",
                document_id="doc-1",
                status=ValidationStatus.COMPLETED,
                standards_set="s",
                findings=[
                    Finding(
                        finding_id="f1", requirement_id="r1", requirement_title="t",
                        severity=Severity.HIGH, status=ComplianceStatus.FAIL,
                        evidence="e" * 1000,
                    )
                ],
            )
        )
        assert written == [("val-1", 1)]
        item = next(
            t.items["val-1"] for t in store._tables.values() if "val-1" in t.items
        )
        assert "e" * 1000 not in str(item)
        assert item["findings_count"] == 1
