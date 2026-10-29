# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The read-only tools the conversational query agent calls.

document_tools, kb_tools and validation_tools were all at 0% coverage. They are
what grounds the agent's answers, so their failure mode is quiet and bad: a tool
that returns "not found" for a document that exists, or that silently drops the
image pages of a scanned PDF, makes the agent confidently answer from nothing.

Every tool resolves its store lazily inside the function body, so the tests
patch the store module attribute and never touch AWS.
"""

from __future__ import annotations

import pytest

from app.agents.tools import document_tools as dt
from app.agents.tools import kb_tools as kbt
from app.agents.tools import validation_tools as vt
from app.models.document import Document, DocumentStatus, DocumentType, ParsedSection
from app.models.validation import ValidationResult, ValidationStatus


def _call(tool, *args, **kwargs):
    """Invoke a @tool-decorated function directly.

    Strands wraps the callable; unwrap it the same way the existing agent tests
    do (see tests/backend/test_calculator_safety.py).
    """
    fn = tool
    for attr in ("_tool_func", "func", "__wrapped__", "original", "_func"):
        candidate = getattr(fn, attr, None)
        if callable(candidate):
            fn = candidate
            break
    return fn(*args, **kwargs)


def _section(page: int, title: str, content: str, images=None) -> ParsedSection:
    return ParsedSection(
        section_id=f"sec-{page}-{title}",
        title=title,
        content=content,
        page_number=page,
        images=list(images or []),
    )


def _doc(sections=None, filename: str = "sdd.pdf") -> Document:
    return Document(
        document_id="doc-1",
        filename=filename,
        s3_key="k",
        document_type=DocumentType.SDD,
        status=DocumentStatus.PARSED,
        hosting_scope="both",
        sections=list(sections or []),
    )


class _FakeStore:
    def __init__(self, doc=None, validations=None):
        self._doc = doc
        self._validations = list(validations or [])

    def get_document(self, document_id):
        return self._doc

    def list_validations(self, document_id=None):
        return list(self._validations)


@pytest.fixture
def store(monkeypatch):
    """Install a fake validation_store that every tool will import lazily."""
    import app.services.validation_store as mod

    holder = {}

    def _install(doc=None, validations=None):
        fake = _FakeStore(doc, validations)
        monkeypatch.setattr(mod, "validation_store", fake)
        holder["store"] = fake
        return fake

    return _install


# ---------------------------------------------------------------------------
# document_tools
# ---------------------------------------------------------------------------

class TestGetDocumentText:
    def test_returns_sections_with_page_markers(self, store):
        store(_doc([
            _section(1, "Overview", "Two datacentres in active-active."),
            _section(2, "Storage", "Synchronous replication between sites."),
        ]))
        out = _call(dt.get_document_text, "doc-1")
        assert "[Page 1] Overview" in out
        assert "[Page 2] Storage" in out
        assert "active-active" in out

    def test_missing_document_points_at_the_image_path(self, store):
        """The agent needs to know a retry via analyze_page_images exists,
        otherwise a scanned PDF just looks like an empty document."""
        store(None)
        out = _call(dt.get_document_text, "doc-1")
        assert "not found" in out.lower()
        assert "analyze_page_images" in out

    def test_unparsed_document_is_treated_the_same(self, store):
        store(_doc([]))
        assert "analyze_page_images" in _call(dt.get_document_text, "doc-1")

    def test_warns_when_little_text_but_images_present(self, store):
        """This is the scanned-PDF case: without the warning the agent answers
        from ~nothing and sounds certain."""
        store(_doc([_section(1, "Scan", "short", images=["b64data"])]))
        out = _call(dt.get_document_text, "doc-1")
        assert "WARNING" in out
        assert "analyze_page_images" in out
        assert "[1]" in out

    def test_notes_image_pages_when_text_is_plentiful(self, store):
        store(_doc([
            _section(1, "Long", "x" * 600, images=["b64"]),
        ]))
        out = _call(dt.get_document_text, "doc-1")
        assert "WARNING" not in out
        assert "1 pages contain images" in out

    def test_no_image_note_when_there_are_no_images(self, store):
        store(_doc([_section(1, "Long", "x" * 600)]))
        out = _call(dt.get_document_text, "doc-1")
        assert "WARNING" not in out
        assert "contain images" not in out

    def test_text_length_is_summed_across_sections(self, store):
        """500 chars spread over several sections is not a scanned document."""
        store(_doc([
            _section(1, "A", "x" * 300, images=["b64"]),
            _section(2, "B", "x" * 300),
        ]))
        assert "WARNING" not in _call(dt.get_document_text, "doc-1")


class TestGetDocumentDiagramsInfo:
    def test_lists_one_entry_per_image(self, store):
        store(_doc([
            _section(3, "Topology", "c", images=["a", "b"]),
            _section(4, "Network", "c", images=["c"]),
        ]))
        out = _call(dt.get_document_diagrams_info, "doc-1")
        assert "Found 3 diagrams" in out
        assert "Page 3: Topology (Diagram 1)" in out
        assert "Page 3: Topology (Diagram 2)" in out
        assert "Page 4: Network (Diagram 1)" in out

    def test_reports_none_when_no_images(self, store):
        store(_doc([_section(1, "Text only", "prose")]))
        assert "No architecture diagrams" in _call(dt.get_document_diagrams_info, "doc-1")

    def test_missing_document_says_no_diagrams(self, store):
        store(None)
        assert "No diagrams available." == _call(dt.get_document_diagrams_info, "doc-1")


class TestGetPageContent:
    def test_returns_the_matching_page(self, store):
        store(_doc([_section(1, "One", "first"), _section(7, "Seven", "seventh")]))
        out = _call(dt.get_page_content, "doc-1", 7)
        assert "[Page 7] Seven" in out
        assert "seventh" in out
        assert "first" not in out

    def test_absent_page_is_reported_not_guessed(self, store):
        store(_doc([_section(1, "One", "first")]))
        assert "No content found for page 9" in _call(dt.get_page_content, "doc-1", 9)

    def test_missing_document(self, store):
        store(None)
        assert _call(dt.get_page_content, "doc-1", 1) == "Document not found."


class TestAnalyzePageImages:
    @pytest.fixture
    def bedrock(self, monkeypatch):
        """Stub boto3.client so no Bedrock call leaves the process."""
        calls: list[dict] = []
        box = {"text": "Extracted: services must span two AZs."}

        class _Client:
            def converse(self, **kw):
                calls.append(kw)
                if isinstance(box["text"], Exception):
                    raise box["text"]
                return {
                    "output": {"message": {"content": [{"text": box["text"]}]}}
                }

        import boto3

        monkeypatch.setattr(boto3, "client", lambda *a, **kw: _Client())
        return calls, box

    def test_analyzes_image_pages_and_returns_model_text(self, store, bedrock):
        calls, _ = bedrock
        import base64

        img = base64.b64encode(b"png-bytes").decode()
        store(_doc([_section(2, "Scan", "s", images=[img])]))
        out = _call(dt.analyze_page_images, "doc-1")
        assert "[Page 2] Scan" in out
        assert "two AZs" in out
        assert len(calls) == 1

    def test_filters_to_the_requested_pages(self, store, bedrock):
        calls, _ = bedrock
        import base64

        img = base64.b64encode(b"x").decode()
        store(_doc([
            _section(1, "One", "s", images=[img]),
            _section(2, "Two", "s", images=[img]),
        ]))
        out = _call(dt.analyze_page_images, "doc-1", "2")
        assert "[Page 2]" in out
        assert "[Page 1]" not in out
        assert len(calls) == 1

    def test_caps_images_per_page(self, store, bedrock):
        """Max 3 per page — an unbounded loop here is a cost bug, one Bedrock
        call per image."""
        calls, _ = bedrock
        import base64

        img = base64.b64encode(b"x").decode()
        store(_doc([_section(1, "Many", "s", images=[img] * 10)]))
        _call(dt.analyze_page_images, "doc-1")
        assert len(calls) == 3

    def test_caps_total_images_across_pages(self, store, bedrock):
        calls, _ = bedrock
        import base64

        img = base64.b64encode(b"x").decode()
        store(_doc([_section(p, f"P{p}", "s", images=[img] * 3) for p in range(1, 6)]))
        _call(dt.analyze_page_images, "doc-1")
        assert len(calls) == 5

    def test_no_images_returns_early_without_calling_bedrock(self, store, bedrock):
        calls, _ = bedrock
        store(_doc([_section(1, "Text", "prose")]))
        assert "No images found" in _call(dt.analyze_page_images, "doc-1")
        assert calls == []

    def test_a_model_error_is_reported_per_page_not_raised(self, store, bedrock):
        """One unreadable page must not lose the other pages' output."""
        calls, box = bedrock
        box["text"] = RuntimeError("throttled")
        import base64

        img = base64.b64encode(b"x").decode()
        store(_doc([_section(1, "P", "s", images=[img])]))
        out = _call(dt.analyze_page_images, "doc-1")
        assert "Error analyzing image" in out
        assert "throttled" in out

    def test_missing_document(self, store, bedrock):
        store(None)
        assert "not found" in _call(dt.analyze_page_images, "doc-1").lower()


# ---------------------------------------------------------------------------
# kb_tools
# ---------------------------------------------------------------------------

class TestKbSearch:
    @pytest.fixture
    def vector(self, monkeypatch):
        import app.services.vector_store as mod

        box = {"results": []}

        class _VS:
            def similarity_search(self, query, document_id="", top_k=5):
                if isinstance(box["results"], Exception):
                    raise box["results"]
                box["last"] = {"query": query, "top_k": top_k}
                return box["results"]

        monkeypatch.setattr(mod, "vector_store", _VS())
        return box

    def test_formats_each_hit_with_score(self, vector):
        vector["results"] = [
            {"section_title": "Replication", "score": 0.91, "text": "sync between DCs"},
            {"section_title": "Failover", "score": 0.72, "text": "manual runbook"},
        ]
        out = _call(kbt.kb_search, "replication")
        assert "[Result 1] Replication — Source: unknown source (Score: 0.91)" in out
        assert "[Result 2] Failover — Source: unknown source (Score: 0.72)" in out
        assert "sync between DCs" in out

    def test_passes_the_result_count_through(self, vector):
        vector["results"] = []
        _call(kbt.kb_search, "q", 9)
        assert vector["last"]["top_k"] == 9

    def test_no_hits_is_stated_plainly(self, vector):
        vector["results"] = []
        assert _call(kbt.kb_search, "q") == "No relevant results found."

    def test_missing_fields_do_not_crash_the_tool(self, vector):
        vector["results"] = [{}]
        out = _call(kbt.kb_search, "q")
        assert "unknown source" in out
        assert "0.00" in out

    def test_every_hit_names_its_source_document_and_page(self, vector):
        """Without a source on each passage the model attributed another
        design's risk register, with invented pages, to the design asked about."""
        vector["results"] = [
            {"score": 0.9, "text": "R-001 Redis", "page_number": 21,
             "source_uri": "s3://b/documents/doc-aaa111/SDD-CBP-2026-001_Core_Banking_Platform.pdf"},
            {"score": 0.8, "text": "HSM-A", "page_number": 0,
             "source_uri": "s3://b/validations/val-bbb222/findings.json"},
        ]
        out = _call(kbt.kb_search, "risks")
        assert "Source: SDD-CBP-2026-001_Core_Banking_Platform.pdf (doc-aaa111), page 21" in out
        assert "Source: validation findings val-bbb222" in out
        assert "Attribute every claim to the source it came from" in out

    def test_parsed_text_is_named_after_its_document(self, monkeypatch):
        from types import SimpleNamespace
        from app.services import validation_store as vs_mod
        monkeypatch.setattr(vs_mod.validation_store, "get_document",
                            lambda doc_id: SimpleNamespace(filename="design.pdf"))
        assert kbt.describe_source("s3://b/documents/doc-ccc333/parsed_content.json") == (
            "design.pdf (doc-ccc333), parsed text"
        )

    def test_long_text_is_truncated(self, vector):
        vector["results"] = [{"section_title": "T", "score": 1.0, "text": "y" * 900}]
        out = _call(kbt.kb_search, "q")
        assert "y" * 500 in out and "y" * 501 not in out

    def test_a_search_failure_is_returned_as_text_not_raised(self, vector):
        """A raise here would abort the agent turn; the agent can recover from a
        message."""
        vector["results"] = RuntimeError("KB unavailable")
        out = _call(kbt.kb_search, "q")
        assert out.startswith("Search failed")
        assert "KB unavailable" in out


class TestGetValidationResultsSummary:
    def test_summarises_each_validation(self, store):
        doc = _doc(filename="design.pdf")
        fake = store(doc, [
            ValidationResult(
                validation_id="val-1", document_id="doc-1",
                status=ValidationStatus.COMPLETED, standards_set="s",
                overall_score=82.5,
            )
        ])
        out = _call(kbt.get_validation_results_summary)
        assert "1 validations" in out
        assert "design.pdf" in out
        assert "score=82.5" in out
        assert "no findings" in out
        assert fake is not None

    def test_empty_state(self, store):
        store(None, [])
        assert "No validations have been run yet." == _call(
            kbt.get_validation_results_summary
        )


# ---------------------------------------------------------------------------
# validation_tools
# ---------------------------------------------------------------------------

class TestGetPastValidations:
    def test_lists_prior_runs_for_the_document(self, store):
        store(None, [
            ValidationResult(
                validation_id="val-a", document_id="doc-1",
                status=ValidationStatus.COMPLETED, standards_set="s",
                overall_score=40.0,
            ),
            ValidationResult(
                validation_id="val-b", document_id="doc-1",
                status=ValidationStatus.FAILED, standards_set="s",
            ),
        ])
        out = _call(vt.get_past_validations, "doc-1")
        assert "2 previous validations" in out
        assert "val-a" in out and "val-b" in out
        assert "score=40.0" in out

    def test_no_history_is_stated(self, store):
        store(None, [])
        assert "No previous validations found" in _call(vt.get_past_validations, "doc-1")
