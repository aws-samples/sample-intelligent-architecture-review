# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The document processing pipeline: queueing, status reporting, and the workers.

The pipeline is what turns an upload into a validatable document. Its failure
modes are all silent: a job that never leaves the queue, a document whose
sections are parsed but never persisted to S3 (so they vanish on the next task
restart and validation has nothing to validate), or a parse failure that leaves
the row stuck in "parsing" forever with no error shown.

Two parse workers run concurrently and a single sync worker runs serially,
because Bedrock allows one KB ingestion job at a time. These tests drive the
worker bodies directly rather than starting the background tasks, so nothing
depends on real timing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.models.document import Document, DocumentStatus, DocumentType
from app.services.pipeline import DocumentPipeline, PipelineJob, PipelineStage


def _doc(document_id="doc-1", **over) -> Document:
    base = dict(
        document_id=document_id,
        filename="sdd.pdf",
        s3_key="k",
        document_type=DocumentType.SDD,
        status=DocumentStatus.UPLOADED,
        hosting_scope="both",
    )
    base.update(over)
    return Document(**base)


class _Store:
    """Minimal validation_store stand-in that records saves."""

    def __init__(self, doc=None):
        self.doc = doc
        self.saves: list[Document] = []

    def get_document(self, document_id):
        return self.doc

    def save_document(self, document):
        self.saves.append(document)
        return document


@pytest.fixture
def pipeline():
    return DocumentPipeline()


@pytest.fixture
def store(monkeypatch):
    import app.services.validation_store as mod

    def _install(doc=None):
        s = _Store(doc)
        monkeypatch.setattr(mod, "validation_store", s)
        return s

    return _install


class TestSubmit:
    async def test_queues_the_job_and_tracks_it(self, pipeline, store):
        store(_doc())
        await pipeline.submit("doc-1", "sdd.pdf", b"%PDF-fake")
        job = pipeline.get_job_status("doc-1")
        assert job is not None
        assert job.stage is PipelineStage.QUEUED
        assert job.filename == "sdd.pdf"
        assert pipeline._parse_queue.qsize() == 1

    async def test_marks_the_document_as_queued_for_the_user(self, pipeline, store):
        """The UI reads processing_step; leaving it blank makes an upload look
        like nothing happened."""
        s = store(_doc())
        await pipeline.submit("doc-1", "sdd.pdf", b"x")
        assert s.saves
        saved = s.saves[-1]
        assert saved.sync_status == "pending"
        assert "Queued" in saved.processing_step

    async def test_a_missing_document_row_does_not_block_queueing(self, pipeline, store):
        """The bytes are already in S3; refusing to queue would lose the upload."""
        store(None)
        await pipeline.submit("doc-1", "sdd.pdf", b"x")
        assert pipeline._parse_queue.qsize() == 1

    async def test_the_standards_flag_is_carried_on_the_job(self, pipeline, store):
        """It selects a different downstream path (rule extraction), so losing it
        means a standards document is treated as an SDD."""
        store(_doc())
        await pipeline.submit("doc-1", "std.pdf", b"x", is_standards_doc=True)
        assert pipeline.get_job_status("doc-1").is_standards_doc is True

    async def test_unknown_job_status_is_none(self, pipeline):
        assert pipeline.get_job_status("never-submitted") is None


class TestQueueStatus:
    async def test_reports_empty_state(self, pipeline):
        status = pipeline.get_queue_status()
        assert status == {
            "parse_queue_size": 0,
            "sync_queue_size": 0,
            "active_jobs": 0,
            "jobs": [],
            "completed_count": 0,
        }

    async def test_reports_queued_jobs(self, pipeline, store):
        store(_doc())
        await pipeline.submit("doc-1", "a.pdf", b"x")
        await pipeline.submit("doc-2", "b.pdf", b"x")
        status = pipeline.get_queue_status()
        assert status["parse_queue_size"] == 2
        assert status["active_jobs"] == 2
        assert {j["document_id"] for j in status["jobs"]} == {"doc-1", "doc-2"}

    async def test_every_job_field_is_json_serialisable(self, pipeline, store):
        """This dict is returned straight from /api/documents/pipeline/status; a
        raw datetime here would 500 the endpoint."""
        import json

        store(_doc())
        await pipeline.submit("doc-1", "a.pdf", b"x")
        job = pipeline.get_job_status("doc-1")
        job.started_at = datetime.utcnow()
        job.completed_at = datetime.utcnow()
        job.error = "something went wrong"
        json.dumps(pipeline.get_queue_status())  # must not raise

    async def test_stage_is_reported_as_a_string(self, pipeline, store):
        store(_doc())
        await pipeline.submit("doc-1", "a.pdf", b"x")
        assert pipeline.get_queue_status()["jobs"][0]["stage"] == "queued"

    def test_completed_count_reflects_finished_jobs(self, pipeline):
        pipeline._completed_jobs.append(
            PipelineJob(
                document_id="d", filename="f", is_standards_doc=False,
                stage=PipelineStage.COMPLETED, queued_at=datetime.utcnow(),
            )
        )
        assert pipeline.get_queue_status()["completed_count"] == 1


class TestStartStop:
    async def test_start_marks_running_and_spawns_workers(self, pipeline, monkeypatch):
        spawned: list = []
        real_create_task = asyncio.create_task

        def _track(coro):
            spawned.append(coro)
            coro.close()  # don't actually run the worker loops

            async def _noop():
                return None

            return real_create_task(_noop())

        monkeypatch.setattr(asyncio, "create_task", _track)
        await pipeline.start()
        assert pipeline._running is True
        # Two parse workers (parallel) + one sync worker (serial, because
        # Bedrock permits a single KB ingestion job at a time).
        assert len(spawned) == 3

    async def test_stop_clears_running(self, pipeline):
        pipeline._running = True
        await pipeline.stop()
        assert pipeline._running is False


class _Parsed:
    """Stand-in for extract_text_and_sections' return value."""

    class _S:
        def __init__(self, i):
            self.title = f"Section {i}"
            self.content = f"content {i}"
            self.page_number = i
            self.level = 1
            self.images = []

    def __init__(self, n=2, page_count=9):
        self.sections = [self._S(i) for i in range(1, n + 1)]
        self.page_count = page_count


class TestParseWorker:
    """The worker body is driven for exactly one queue item.

    `_running` is flipped off inside the patched parser so the `while` loop
    exits after a single pass instead of blocking on the next 5s queue wait.
    """

    @pytest.fixture
    def wired(self, pipeline, monkeypatch):
        import app.services.validation_store as vs_mod
        import app.utils.pdf_parser as parser_mod
        import app.services.document_store as ds_mod
        import app.services.tier_classifier as tier_mod

        state: dict = {"parsed_saved": [], "tier": None}

        def _install(doc, parse_result=None, parse_error=None, s3_error=None):
            store = _Store(doc)
            monkeypatch.setattr(vs_mod, "validation_store", store)

            def _parse(pdf_bytes):
                pipeline._running = False  # single pass through the worker loop
                if parse_error:
                    raise parse_error
                return parse_result or _Parsed()

            monkeypatch.setattr(parser_mod, "extract_text_and_sections", _parse)

            class _DS:
                def save_parsed_content(self, document_id, sections, components):
                    if s3_error:
                        raise s3_error
                    state["parsed_saved"].append((document_id, len(sections)))
                    return True

            monkeypatch.setattr(ds_mod, "document_store", _DS())
            monkeypatch.setattr(
                tier_mod, "classify_tier", lambda d: state["tier"]
            )
            return store, state

        return _install

    async def test_parsed_sections_are_written_to_the_document(self, pipeline, wired):
        doc = _doc()
        store, _ = wired(doc)
        pipeline._running = True
        await pipeline._parse_queue.put(
            (
                PipelineJob(
                    document_id="doc-1", filename="f.pdf", is_standards_doc=False,
                    stage=PipelineStage.QUEUED, queued_at=datetime.utcnow(),
                ),
                b"%PDF",
            )
        )
        await pipeline._parse_worker()
        assert len(doc.sections) == 2
        assert doc.page_count == 9
        assert doc.parsed_at is not None

    async def test_sections_are_persisted_to_s3(self, pipeline, wired):
        """save_document only writes a slim DDB row and in-memory content dies
        with the task, so skipping this leaves validation with nothing to read."""
        doc = _doc()
        _, state = wired(doc)
        pipeline._running = True
        await pipeline._parse_queue.put(
            (
                PipelineJob(
                    document_id="doc-1", filename="f.pdf", is_standards_doc=False,
                    stage=PipelineStage.QUEUED, queued_at=datetime.utcnow(),
                ),
                b"%PDF",
            )
        )
        await pipeline._parse_worker()
        assert state["parsed_saved"] == [("doc-1", 2)]

    async def test_an_s3_failure_does_not_abort_the_job(self, pipeline, wired):
        """Losing durability is bad but recoverable; dropping the whole parse is
        worse, and the document is still usable in this task."""
        doc = _doc()
        wired(doc, s3_error=RuntimeError("S3 down"))
        pipeline._running = True
        await pipeline._parse_queue.put(
            (
                PipelineJob(
                    document_id="doc-1", filename="f.pdf", is_standards_doc=False,
                    stage=PipelineStage.QUEUED, queued_at=datetime.utcnow(),
                ),
                b"%PDF",
            )
        )
        await pipeline._parse_worker()
        assert len(doc.sections) == 2

    async def test_tier_classification_runs_for_sdds(self, pipeline, wired):
        doc = _doc()
        _, state = wired(doc)
        state["tier"] = 2
        pipeline._running = True
        await pipeline._parse_queue.put(
            (
                PipelineJob(
                    document_id="doc-1", filename="f.pdf", is_standards_doc=False,
                    stage=PipelineStage.QUEUED, queued_at=datetime.utcnow(),
                ),
                b"%PDF",
            )
        )
        await pipeline._parse_worker()
        assert doc.declared_tier == 2

    async def test_tier_classification_is_skipped_for_standards_documents(
        self, pipeline, wired
    ):
        """A standards document is not a system — it describes rules that apply
        to systems, so it has no tier."""
        doc = _doc()
        _, state = wired(doc)
        state["tier"] = 3
        pipeline._running = True
        await pipeline._parse_queue.put(
            (
                PipelineJob(
                    document_id="doc-1", filename="std.pdf", is_standards_doc=True,
                    stage=PipelineStage.QUEUED, queued_at=datetime.utcnow(),
                ),
                b"%PDF",
            )
        )
        await pipeline._parse_worker()
        assert doc.declared_tier is None

    async def test_an_existing_tier_is_not_overwritten(self, pipeline, wired):
        """A user override must survive re-parsing."""
        doc = _doc(declared_tier=1, tier_source="user")
        _, state = wired(doc)
        state["tier"] = 4
        pipeline._running = True
        await pipeline._parse_queue.put(
            (
                PipelineJob(
                    document_id="doc-1", filename="f.pdf", is_standards_doc=False,
                    stage=PipelineStage.QUEUED, queued_at=datetime.utcnow(),
                ),
                b"%PDF",
            )
        )
        await pipeline._parse_worker()
        assert doc.declared_tier == 1
        assert doc.tier_source == "user"

    async def test_a_parse_failure_records_the_error_on_the_job(self, pipeline, wired):
        """Without this the row sits in "parsing" forever and the user is never
        told why."""
        doc = _doc()
        wired(doc, parse_error=ValueError("corrupt PDF"))
        job = PipelineJob(
            document_id="doc-1", filename="f.pdf", is_standards_doc=False,
            stage=PipelineStage.QUEUED, queued_at=datetime.utcnow(),
        )
        pipeline._running = True
        await pipeline._parse_queue.put((job, b"%PDF"))
        await pipeline._parse_worker()
        assert job.stage is PipelineStage.FAILED
        assert "corrupt PDF" in (job.error or "")

    async def test_the_worker_exits_when_stopped(self, pipeline):
        """A worker that ignores _running would keep polling after shutdown."""
        pipeline._running = False
        await asyncio.wait_for(pipeline._parse_worker(), timeout=1.0)
