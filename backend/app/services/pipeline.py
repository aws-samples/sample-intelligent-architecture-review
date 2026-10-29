# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document processing pipeline with serial KB sync queue."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class PipelineStage(str, Enum):
    QUEUED = "queued"
    PARSING = "parsing"
    EXTRACTING_STANDARDS = "extracting_standards"
    KB_SYNCING = "kb_syncing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PipelineJob:
    document_id: str
    filename: str
    is_standards_doc: bool
    stage: PipelineStage
    queued_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


class DocumentPipeline:
    """
    Manages document processing pipeline with:
    - Concurrent PDF parsing (multiple docs can parse simultaneously)
    - Serial KB sync (only one ingestion job at a time per Bedrock KB limitation)
    - Status tracking for each document
    - Queue visualization
    """

    def __init__(self):
        self._parse_queue: asyncio.Queue = asyncio.Queue()
        self._sync_queue: asyncio.Queue = asyncio.Queue()
        self._active_jobs: dict[str, PipelineJob] = {}
        self._completed_jobs: list[PipelineJob] = []
        self._sync_lock = asyncio.Lock()
        self._running = False

    async def start(self):
        """Start the pipeline workers."""
        self._running = True
        # Start multiple parse workers (parallel parsing)
        asyncio.create_task(self._parse_worker())
        asyncio.create_task(self._parse_worker())
        # Start single sync worker (serial KB sync)
        asyncio.create_task(self._sync_worker())
        logger.info("Document pipeline started")

    async def stop(self):
        """Stop the pipeline workers."""
        self._running = False
        logger.info("Document pipeline stopped")

    async def submit(
        self,
        document_id: str,
        filename: str,
        pdf_bytes: bytes,
        is_standards_doc: bool = False,
    ):
        """Submit a document for processing."""
        job = PipelineJob(
            document_id=document_id,
            filename=filename,
            is_standards_doc=is_standards_doc,
            stage=PipelineStage.QUEUED,
            queued_at=datetime.utcnow(),
        )
        self._active_jobs[document_id] = job

        # Update document status in store
        from app.services.validation_store import validation_store

        document = validation_store.get_document(document_id)
        if document:
            document.sync_status = "pending"
            document.processing_step = "Queued for processing"
            document.queued_at = job.queued_at
            validation_store.save_document(document)

        await self._parse_queue.put((job, pdf_bytes))
        logger.info(f"Document {document_id} queued for processing")

    def get_job_status(self, document_id: str) -> Optional[PipelineJob]:
        """Get current status of a pipeline job."""
        return self._active_jobs.get(document_id)

    def get_queue_status(self) -> dict:
        """Get overall queue status for frontend display."""
        active = list(self._active_jobs.values())
        return {
            "parse_queue_size": self._parse_queue.qsize(),
            "sync_queue_size": self._sync_queue.qsize(),
            "active_jobs": len(active),
            "jobs": [
                {
                    "document_id": j.document_id,
                    "filename": j.filename,
                    "stage": j.stage.value,
                    "queued_at": j.queued_at.isoformat(),
                    "started_at": j.started_at.isoformat() if j.started_at else None,
                    "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                    "is_standards_doc": j.is_standards_doc,
                    "error": j.error,
                }
                for j in active
            ],
            "completed_count": len(self._completed_jobs),
        }

    async def _parse_worker(self):
        """Worker that handles PDF parsing (can run in parallel)."""
        while self._running:
            try:
                job, pdf_bytes = await asyncio.wait_for(
                    self._parse_queue.get(), timeout=5.0
                )
                job.stage = PipelineStage.PARSING
                job.started_at = datetime.utcnow()

                try:
                    from app.utils.pdf_parser import extract_text_and_sections
                    from app.services.validation_store import validation_store
                    from app.models.document import ParsedSection

                    # Update status to parsing
                    document = validation_store.get_document(job.document_id)
                    if document:
                        document.sync_status = "parsing"
                        document.processing_step = "Parsing PDF content..."
                        validation_store.save_document(document)

                    # Parse PDF
                    parsed = extract_text_and_sections(pdf_bytes)

                    # Update document with parsed content
                    document = validation_store.get_document(job.document_id)
                    if document:
                        document.sections = [
                            ParsedSection(
                                section_id=f"sec-{i}",
                                title=s.title,
                                content=s.content,
                                page_number=s.page_number,
                                level=s.level,
                                images=s.images,
                            )
                            for i, s in enumerate(parsed.sections)
                        ]
                        document.page_count = parsed.page_count
                        document.status = "parsed"
                        document.sync_status = "parsing"
                        document.processing_step = "PDF parsed, queuing for KB sync"
                        document.parsed_at = datetime.utcnow()
                        validation_store.save_document(document)

                        # Persist full sections to S3. save_document only
                        # writes a slim DDB item (no section content, 400KB
                        # limit), and in-memory content is lost on task
                        # restart — so without this, get_document falls back
                        # to an empty sections list and validation has
                        # nothing to validate. (Previously done by the now-
                        # removed _process_document; the pipeline is the
                        # single parse owner and must own this too.)
                        try:
                            from app.services.document_store import document_store
                            document_store.save_parsed_content(
                                job.document_id,
                                document.sections,
                                document.components or [],
                            )
                        except Exception:
                            logger.exception(
                                f"Failed to persist parsed_content to S3 for "
                                f"{job.document_id}; sections will not survive "
                                f"a task restart"
                            )

                    # Tier classification — SDDs only, after parsing.
                    # The standards document itself isn't a system; it
                    # describes rules that apply to systems, so it
                    # has no tier. Failures are non-fatal: the user
                    # can override on the document detail page.
                    if document and not job.is_standards_doc and document.declared_tier is None:
                        try:
                            from app.services.tier_classifier import classify_tier
                            tier = classify_tier(document)
                            if tier is not None:
                                document.declared_tier = tier
                                document.tier_source = "auto"
                                validation_store.save_document(document)
                                logger.info(
                                    f"Auto-classified {job.document_id} as Tier {tier}"
                                )
                        except Exception:
                            logger.exception(
                                f"Tier classification crashed for {job.document_id}; "
                                "leaving declared_tier=None"
                            )

                    # If standards doc, run rule extraction before KB sync.
                    # PDF/PPTX standards uploads route through this pipeline
                    # with is_standards_doc=True (only image uploads take the
                    # route-level direct path). This block previously only set
                    # a status label and fell straight through to KB sync, so
                    # the extraction agent was never invoked — uploads parsed
                    # and synced but produced zero rules. We await the same
                    # _trigger_extraction the image branch uses; it runs the
                    # chunked Strands agent off the event loop via
                    # asyncio.to_thread and records its own progress/live-log.
                    if job.is_standards_doc:
                        job.stage = PipelineStage.EXTRACTING_STANDARDS
                        if document:
                            document.sync_status = "extracting"
                            document.processing_step = "Extracting standards rules..."
                            validation_store.save_document(document)
                        try:
                            # Lazy import: the documents route imports this
                            # module at load time, so importing it here (not at
                            # module top) avoids a circular import.
                            from app.api.routes.documents import _trigger_extraction
                            await _trigger_extraction(job.document_id, job.filename)
                        except Exception:
                            logger.exception(
                                f"Standards extraction failed for "
                                f"{job.document_id}; continuing to KB sync"
                            )

                    # Queue for KB sync
                    await self._sync_queue.put(job)
                    logger.info(f"Document {job.document_id} parsed, queued for KB sync")

                except Exception as e:
                    job.stage = PipelineStage.FAILED
                    job.error = str(e)
                    logger.error(f"Parse failed for {job.document_id}: {e}")

                    # Update document with failure
                    from app.services.validation_store import validation_store

                    document = validation_store.get_document(job.document_id)
                    if document:
                        document.sync_status = "failed"
                        document.sync_error = str(e)
                        document.processing_step = f"Failed during parsing: {e}"
                        validation_store.save_document(document)

            except asyncio.TimeoutError:
                continue

    async def _sync_worker(self):
        """Worker that handles KB sync (runs serially - one at a time)."""
        while self._running:
            try:
                job = await asyncio.wait_for(self._sync_queue.get(), timeout=5.0)

                async with self._sync_lock:
                    job.stage = PipelineStage.KB_SYNCING

                    try:
                        from app.services.vector_store import vector_store
                        from app.services.validation_store import validation_store

                        # Update document status
                        document = validation_store.get_document(job.document_id)
                        if document:
                            document.sync_status = "syncing"
                            document.sync_started_at = datetime.utcnow()
                            document.processing_step = "Syncing to Knowledge Base..."
                            validation_store.save_document(document)

                        # Trigger KB sync
                        if document and document.sections:
                            chunks = [
                                {
                                    "text": section.content,
                                    "section_title": section.title,
                                    "page_number": section.page_number,
                                }
                                for section in document.sections
                            ]
                            vector_store.index_document_chunks(
                                job.document_id, chunks
                            )

                        # Poll for sync completion (max 120s)
                        for _ in range(24):  # 24 * 5s = 120s max
                            await asyncio.sleep(5)
                            # In production, poll get_ingestion_job for status
                            # For now, assume sync completes after first check
                            break

                        # Mark as synced
                        job.stage = PipelineStage.COMPLETED
                        job.completed_at = datetime.utcnow()

                        document = validation_store.get_document(job.document_id)
                        if document:
                            document.sync_status = "synced"
                            document.processing_step = "Fully synced to Knowledge Base"
                            document.sync_completed_at = datetime.utcnow()
                            validation_store.save_document(document)

                        # Move to completed
                        self._completed_jobs.append(job)
                        if job.document_id in self._active_jobs:
                            del self._active_jobs[job.document_id]

                        logger.info(f"Document {job.document_id} fully synced")

                    except Exception as e:
                        job.stage = PipelineStage.FAILED
                        job.error = str(e)
                        logger.error(f"KB sync failed for {job.document_id}: {e}")

                        from app.services.validation_store import validation_store

                        document = validation_store.get_document(job.document_id)
                        if document:
                            document.sync_status = "failed"
                            document.sync_error = str(e)
                            document.processing_step = f"Failed during KB sync: {e}"
                            validation_store.save_document(document)

            except asyncio.TimeoutError:
                continue


# Singleton instance
pipeline = DocumentPipeline()
