# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document-serving routes: file bytes, slide PNGs, sync status, diagrams.

These endpoints feed the document viewer, so their failure modes are visible to
the user but easy to miss in code: the wrong media type makes the PDF iframe
offer a download prompt instead of rendering (the PPTX case, where the stored
object is a converted PDF but the filename still ends in .pptx), and a missing
S3 object must 404 rather than serve an empty body that renders as a blank page.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.models.document import (
    Document,
    DocumentStatus,
    DocumentType,
    ParsedSection,
    SlideStatus,
)


def _doc(**over) -> Document:
    base = dict(
        document_id="doc-1",
        filename="design.pdf",
        s3_key="documents/doc-1/design.pdf",
        document_type=DocumentType.SDD,
        status=DocumentStatus.PARSED,
        hosting_scope="both",
    )
    base.update(over)
    return Document(**base)


def _section(page=1, images=None, title="Topology") -> ParsedSection:
    return ParsedSection(
        section_id=f"s{page}",
        title=title,
        content="body",
        page_number=page,
        images=list(images or []),
    )


class _Store:
    def __init__(self, doc=None):
        self.doc = doc

    def get_document(self, document_id):
        return self.doc


class _DocStore:
    def __init__(self, objects=None, error=None):
        self.objects = objects or {}
        self.error = error
        self.requested: list[str] = []

    def download_document(self, s3_key):
        self.requested.append(s3_key)
        if self.error:
            raise self.error
        return self.objects.get(s3_key)


@pytest.fixture
def client():
    from app.main import app
    from app.api.auth import get_current_user
    from app.api.dependencies import get_validation_store, get_document_store

    def _make(doc=None, objects=None, error=None):
        ds = _DocStore(objects, error)
        app.dependency_overrides[get_current_user] = lambda: {
            "sub": "s", "email": "u@x.com"
        }
        app.dependency_overrides[get_validation_store] = lambda: _Store(doc)
        app.dependency_overrides[get_document_store] = lambda: ds
        return TestClient(app), ds

    yield _make

    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_validation_store, None)
    app.dependency_overrides.pop(get_document_store, None)


class TestServeDocumentFile:
    def test_pdf_is_served_with_the_pdf_media_type(self, client):
        c, _ = client(
            doc=_doc(), objects={"documents/doc-1/design.pdf": b"%PDF-1.7 body"}
        )
        resp = c.get("/api/documents/doc-1/pdf")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == b"%PDF-1.7 body"

    def test_served_inline_so_the_viewer_renders_it(self, client):
        """`attachment` here would make the iframe prompt a download."""
        c, _ = client(doc=_doc(), objects={"documents/doc-1/design.pdf": b"%PDF"})
        resp = c.get("/api/documents/doc-1/pdf")
        assert resp.headers["content-disposition"].startswith("inline")
        assert "design.pdf" in resp.headers["content-disposition"]

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("scan.png", "image/png"),
            ("scan.jpg", "image/jpeg"),
            ("scan.jpeg", "image/jpeg"),
            ("scan.tiff", "image/tiff"),
            ("scan.bmp", "image/bmp"),
        ],
    )
    def test_image_uploads_get_their_image_type(self, client, filename, expected):
        key = f"documents/doc-1/{filename}"
        c, _ = client(doc=_doc(filename=filename, s3_key=key), objects={key: b"img"})
        assert c.get("/api/documents/doc-1/pdf").headers["content-type"] == expected

    def test_an_unknown_extension_falls_back_to_octet_stream(self, client):
        key = "documents/doc-1/notes.xyz"
        c, _ = client(doc=_doc(filename="notes.xyz", s3_key=key), objects={key: b"x"})
        assert c.get("/api/documents/doc-1/pdf").headers["content-type"] == (
            "application/octet-stream"
        )

    def test_a_filename_without_an_extension_is_handled(self, client):
        key = "documents/doc-1/README"
        c, _ = client(doc=_doc(filename="README", s3_key=key), objects={key: b"x"})
        assert c.get("/api/documents/doc-1/pdf").status_code == 200

    def test_a_pptx_upload_is_served_as_pdf(self, client):
        """filename stays .pptx for display, but s3_key points at the converted
        PDF the iframe actually renders — so the media type must follow the
        stored object, not the name."""
        key = "documents/doc-1/deck.pdf"
        c, _ = client(
            doc=_doc(filename="deck.pptx", s3_key=key, source_format="pptx",
                     slide_count=3),
            objects={key: b"%PDF converted"},
        )
        resp = c.get("/api/documents/doc-1/pdf")
        assert resp.headers["content-type"] == "application/pdf"

    def test_unknown_document_is_404(self, client):
        c, _ = client(doc=None)
        assert c.get("/api/documents/doc-1/pdf").status_code == 404

    def test_a_missing_object_is_404_not_an_empty_body(self, client):
        """An empty 200 renders as a blank viewer with no explanation."""
        c, _ = client(doc=_doc(), objects={})
        assert c.get("/api/documents/doc-1/pdf").status_code == 404

    def test_a_storage_error_is_a_500(self, client):
        c, _ = client(doc=_doc(), error=RuntimeError("S3 unreachable"))
        assert c.get("/api/documents/doc-1/pdf").status_code == 500


class TestServeSlide:
    def _pptx_doc(self):
        return _doc(
            filename="deck.pptx",
            s3_key="documents/doc-1/deck.pdf",
            source_format="pptx",
            slide_count=2,
            slides=[
                SlideStatus(slide_index=1, slide_type="text_heavy", status="extracted"),
                SlideStatus(slide_index=2, slide_type="diagram_heavy", status="extracted"),
            ],
        )

    def test_serves_the_slide_png(self, client):
        key = "documents/doc-1/slides/2.png"
        c, ds = client(doc=self._pptx_doc(), objects={key: b"\x89PNG slide two"})
        resp = c.get("/api/documents/doc-1/slides/2")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == b"\x89PNG slide two"
        assert ds.requested == [key]

    def test_the_index_is_one_based(self, client):
        """Keys are written 1-based by the rasteriser; an off-by-one here shows
        the wrong slide next to each rule."""
        c, ds = client(
            doc=self._pptx_doc(), objects={"documents/doc-1/slides/1.png": b"png"}
        )
        c.get("/api/documents/doc-1/slides/1")
        assert ds.requested == ["documents/doc-1/slides/1.png"]

    def test_slides_are_cacheable(self, client):
        """Rasterised slides never change for a given document."""
        c, _ = client(
            doc=self._pptx_doc(), objects={"documents/doc-1/slides/1.png": b"png"}
        )
        resp = c.get("/api/documents/doc-1/slides/1")
        assert "max-age" in resp.headers["cache-control"]

    def test_a_non_pptx_document_has_no_slides(self, client):
        c, _ = client(doc=_doc())
        resp = c.get("/api/documents/doc-1/slides/1")
        assert resp.status_code == 404
        assert "not a PPTX" in resp.json()["detail"]

    def test_a_missing_slide_object_is_404(self, client):
        c, _ = client(doc=self._pptx_doc(), objects={})
        resp = c.get("/api/documents/doc-1/slides/9")
        assert resp.status_code == 404
        assert "9" in resp.json()["detail"]

    def test_unknown_document_is_404(self, client):
        c, _ = client(doc=None)
        assert c.get("/api/documents/doc-1/slides/1").status_code == 404


class TestSyncStatus:
    def test_reports_the_documents_pipeline_fields(self, client):
        c, _ = client(doc=_doc(sync_status="parsing",
                               processing_step="Parsing PDF content..."))
        body = c.get("/api/documents/doc-1/sync-status").json()
        assert body["document_id"] == "doc-1"
        assert body["filename"] == "design.pdf"
        assert body["sync_status"] == "parsing"
        assert body["processing_step"] == "Parsing PDF content..."

    def test_timestamps_are_iso_strings_or_null(self, client):
        """These are read straight into JSON; a raw datetime would 500."""
        from datetime import datetime

        c, _ = client(doc=_doc(queued_at=datetime(2026, 7, 28, 12, 0, 0)))
        body = c.get("/api/documents/doc-1/sync-status").json()
        assert body["queued_at"] == "2026-07-28T12:00:00"
        assert body["sync_started_at"] is None
        assert body["sync_completed_at"] is None

    def test_pipeline_stage_is_null_when_no_job_is_active(self, client):
        c, _ = client(doc=_doc())
        assert c.get("/api/documents/doc-1/sync-status").json()["pipeline_stage"] is None

    def test_pipeline_stage_is_reported_when_a_job_exists(self, client, monkeypatch):
        from datetime import datetime
        import app.api.routes.documents as mod
        from app.services.pipeline import PipelineJob, PipelineStage

        job = PipelineJob(
            document_id="doc-1", filename="design.pdf", is_standards_doc=False,
            stage=PipelineStage.PARSING, queued_at=datetime.utcnow(),
        )
        monkeypatch.setattr(mod.pipeline, "get_job_status", lambda d: job)
        c, _ = client(doc=_doc())
        assert c.get("/api/documents/doc-1/sync-status").json()["pipeline_stage"] == (
            "parsing"
        )

    def test_unknown_document_is_404(self, client):
        c, _ = client(doc=None)
        assert c.get("/api/documents/doc-1/sync-status").status_code == 404


class TestDiagrams:
    def _b64(self) -> str:
        return base64.b64encode(b"fake-png").decode()

    def test_returns_one_entry_per_embedded_image(self, client):
        img = self._b64()
        c, _ = client(doc=_doc(sections=[
            _section(3, images=[img, img], title="Topology"),
            _section(4, images=[img], title="Network"),
        ]))
        body = c.get("/api/documents/doc-1/diagrams").json()
        assert body["total"] == 3
        assert len(body["diagrams"]) == 3

    def test_images_are_returned_as_data_urls(self, client):
        """The viewer renders these directly; a bare base64 blob would not
        display."""
        img = self._b64()
        c, _ = client(doc=_doc(sections=[_section(1, images=[img])]))
        first = c.get("/api/documents/doc-1/diagrams").json()["diagrams"][0]
        assert first["image_url"].startswith("data:image/png;base64,")
        assert img in first["image_url"]

    def test_each_diagram_carries_its_page_and_caption(self, client):
        c, _ = client(doc=_doc(sections=[_section(7, images=[self._b64()],
                                                 title="DR Layout")]))
        first = c.get("/api/documents/doc-1/diagrams").json()["diagrams"][0]
        assert first["page_number"] == 7
        assert "DR Layout" in first["caption"]

    def test_a_document_with_no_images_returns_an_empty_list(self, client):
        c, _ = client(doc=_doc(sections=[_section(1)]))
        body = c.get("/api/documents/doc-1/diagrams").json()
        assert body == {"diagrams": [], "total": 0}

    def test_an_unparsed_document_returns_an_empty_list(self, client):
        c, _ = client(doc=_doc(sections=[]))
        assert c.get("/api/documents/doc-1/diagrams").json()["total"] == 0

    def test_unknown_document_is_404(self, client):
        c, _ = client(doc=None)
        assert c.get("/api/documents/doc-1/diagrams").status_code == 404
