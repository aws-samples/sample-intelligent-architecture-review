# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document upload and management routes."""

import hashlib
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from app.api.auth import get_current_user
from app.api.dependencies import (
    get_document_store,
    get_validation_store,
    get_vector_store,
)
from app.api.rbac import Role, require_owner_or_admin, require_role, resolve_role
from app.models.document import Document, DocumentStatus, DocumentType, DocumentUploadResponse
from app.services.document_store import DocumentStore
from app.services.pipeline import pipeline
from app.services.validation_store import ValidationStore
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

router = APIRouter()


# NOTE: _process_document was removed. It ran a SECOND concurrent parse
# that raced the DocumentPipeline on document.status (the cause of the
# "status: parsing" vs "parsed" mismatch users hit before curation/
# validation). The pipeline is now the single owner of the
# parse → status → S3-persist → KB-sync flow; see
# app/services/pipeline.py::_parse_worker. Standards auto-extraction still
# runs via _trigger_extraction on the standards upload branch below.


async def _trigger_extraction(document_id: str, filename: str = ""):
    """Trigger standards extraction for a document.

    Dispatches to the unified multimodal, parallel extractor
    (agents.extraction.run.run_extraction) for BOTH PDF and PPTX, so
    there is no format-divergent path: ingest -> parallel extract ->
    dedup. Live-log job registration + terminal marks are preserved so
    the frontend banner keeps working.
    """


    try:
        from app.agents.extraction.run import run_extraction
        from app.models.document import SourceFormat
        from app.services.document_store import document_store as _doc_store
        from app.services.extraction_log import (
            mark_completed,
            mark_extracting,
            mark_failed,
            start_job,
        )
        from app.services.validation_store import validation_store as _val_store

        from app.services.extraction_log import is_in_flight

        document = _val_store.get_document(document_id)
        if document is None:
            logger.error(
                f"Cannot trigger extraction — document {document_id} not found in store"
            )
            return

        # Start-time claim. The request-side guard on /standards/extract can miss
        # a race, because background tasks run AFTER the response: a manual run
        # requested while the doc is still parsing sees no in-flight state yet.
        # This check sits immediately before start_job with no await between, so
        # on one event loop the (lock-protected) check-and-claim is atomic; the
        # cross-task document-row stamp covers the multi-ECS-task case. Skips the
        # duplicate rather than minting a second batch.
        if is_in_flight(document_id):
            logger.info(
                f"Extraction already in flight for {document_id}; skipping duplicate auto-trigger"
            )
            return

        # Register with the live-log so the frontend banner + doc-detail log
        # panel can poll progress.
        start_job(document_id, document.filename)
        mark_extracting(document_id)

        # Fetch the source bytes for the right format. PPTX needs the original
        # .pptx (for native text + structured tables) AND the converted PDF
        # (stored at document.s3_key by the upload route) so slides can be
        # rasterised locally without the sidecar. PDF needs its bytes.
        pptx_bytes = None
        pdf_bytes = None
        try:
            if getattr(document, "source_format", SourceFormat.PDF) == SourceFormat.PPTX:
                if not document.original_s3_key:
                    raise ValueError("PPTX document has no original_s3_key")
                pptx_bytes = _doc_store.download_document(document.original_s3_key)
                # Converted PDF (best-effort — ingest degrades to text-only
                # if slide PNGs can't be produced).
                try:
                    pdf_bytes = _doc_store.download_document(document.s3_key)
                except Exception:
                    logger.warning(f"converted PDF absent for {document_id}; "
                                   "slides may render text-only")
            else:
                pdf_bytes = _doc_store.download_document(document.s3_key)
        except Exception as fetch_exc:
            logger.exception(f"Failed to fetch source bytes for {document_id}")
            mark_failed(document_id, str(fetch_exc))
            return

        try:
            summary = await run_extraction(
                document, _doc_store, _val_store,
                pptx_bytes=pptx_bytes, pdf_bytes=pdf_bytes,
            )
            mark_completed(document_id, summary.get("rules_saved", 0))
            logger.info(f"Extraction complete for {document_id}: {summary}")
        except Exception as run_exc:
            logger.exception(f"Extraction failed for {document_id}")
            mark_failed(document_id, str(run_exc))
        return

    except Exception as e:
        logger.error(f"Failed to trigger extraction: {e}")


@router.post("", response_model=DocumentUploadResponse)
async def upload_document(
    response: Response,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    document_type: Optional[str] = None,
    extraction_notes: Optional[str] = None,
    hosting_scope: Optional[str] = None,
    doc_store: DocumentStore = Depends(get_document_store),
    val_store: ValidationStore = Depends(get_validation_store),
    vec_store: VectorStore = Depends(get_vector_store),
    claims: dict = Depends(get_current_user),
):
    """
    Upload a PDF document for HA validation.

    The document will be stored in S3 and processed asynchronously.

    Args:
        file: PDF file to upload
        document_type: Optional document type ('sdd' or 'standards'), defaults to 'sdd'
        extraction_notes: Free-form NLP context describing how this
            standards document is organised — page-level relationships, scope,
            substitutions/exceptions location. When non-empty, the dispatcher
            routes to the guided extraction agent which uses these notes
            verbatim as authoritative document context. Only meaningful for
            standards documents; ignored for SDDs.
    """
    # Validate file type. .pptx is supported; the older binary .ppt is
    # intentionally NOT supported (LibreOffice's binary-PPT path is
    # buggy and the format is effectively deprecated in many enterprises
    # estates). Users should re-save as .pptx.
    ALLOWED_EXTENSIONS = (".pdf", ".pptx", ".png", ".jpg", ".jpeg", ".tiff", ".bmp")
    if not file.filename or not any(file.filename.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Accepted: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    # RBAC — the upload endpoint is shared between two document types with
    # DIFFERENT permissions, so the check is by document_type here rather than
    # a single router-level role. A standards doc feeds the rule library
    # (curator+); an SDD is a validation input (validator+). Admin passes both.
    role = resolve_role(claims)
    is_standards = (document_type or "sdd").lower() == DocumentType.STANDARDS.value
    if is_standards:
        allowed = {Role.ADMIN.value, Role.CURATOR.value}
        need = "curator"
    else:
        allowed = {Role.ADMIN.value, Role.VALIDATOR.value}
        need = "validator"
    if role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Uploading a {'standards' if is_standards else 'SDD'} document "
                f"requires the {need} role (or admin). Your role: {role}."
            ),
        )

    # Read file content
    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Payload size cap for .pptx (other types not capped
    # here because PDFs/images flow straight to S3; .pptx feeds the
    # sidecar's tmpfs).
    PPTX_MAX_BYTES = int(os.getenv("PPTX_MAX_BYTES", str(100 * 1024 * 1024)))
    is_pptx = file.filename.lower().endswith(".pptx")
    if is_pptx and len(file_bytes) > PPTX_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f".pptx is {len(file_bytes)} bytes; PPTX_MAX_BYTES={PPTX_MAX_BYTES}. "
                "Trim slides or lower resolution and try again."
            ),
        )

    is_image = any(file.filename.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".tiff", ".bmp"))

    # Validate and set document type
    doc_type = DocumentType.SDD
    if document_type:
        if document_type.lower() == "standards":
            doc_type = DocumentType.STANDARDS
        elif document_type.lower() == "sdd":
            doc_type = DocumentType.SDD
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid document_type '{document_type}'. Must be 'sdd' or 'standards'.",
            )

    # Free-form NLP extraction context. Only meaningful for
    # standards documents; quietly ignored for SDDs (no error so a
    # client mistake is recoverable). Whitespace-only strings are
    # treated as absent.
    notes = (extraction_notes or "").strip() or None
    if notes and doc_type != DocumentType.STANDARDS:
        notes = None  # silently ignored for non-standards uploads
    notes_hash: Optional[str] = (
        hashlib.sha256(notes.encode("utf-8")).hexdigest() if notes else None
    )

    # hosting_scope is required. The Document model 422s
    # if we try to construct without it; check at the route boundary
    # so the error is friendlier than Pydantic's structural one.
    _VALID_SCOPES = {"on_prem", "cloud", "both"}
    scope_norm = (hosting_scope or "").strip().lower().replace("-", "_") or None
    if scope_norm is None:
        raise HTTPException(
            status_code=400,
            detail="hosting_scope is required. Pass one of 'on_prem', 'cloud', or 'both'.",
        )
    if scope_norm not in _VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid hosting_scope '{hosting_scope}'. Must be one of 'on_prem', 'cloud', or 'both'.",
        )

    # Idempotency: dedup ONLY for SDDs. The same SDD uploaded twice is
    # almost always an accident — return the existing record so the
    # validation history stays linked to one document_id. Standards
    # docs intentionally skip this: the input space (notes × hosting
    # scope × tier coverage × layout × relations × extra notes) is
    # too combinatorial to dedup meaningfully, and the user expects
    # every upload to produce a fresh extraction run with its own
    # audit trail. Each Standards upload always mints a new doc-{uuid}.
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    if doc_type == DocumentType.SDD:
        existing = val_store.get_document_by_hash(
            content_hash,
            document_type=doc_type.value,
            extraction_notes_hash=notes_hash,
        )
        if existing:
            logger.info(
                f"SDD upload deduplicated: returning existing document {existing.document_id} "
                f"for content_hash={content_hash[:12]}…"
            )
            response.status_code = 200
            return DocumentUploadResponse(
                document_id=existing.document_id,
                filename=existing.filename,
                status=existing.status,
                message="Document already exists; reusing existing record",
            )

    # Generate document ID and S3 key
    document_id = f"doc-{uuid.uuid4().hex[:12]}"

    # PPTX uploads:
    #  1. Save the original .pptx to documents/{id}/original.pptx
    #     so the user can download it later.
    #  2. Run python-pptx to extract native slide metadata.
    #  3. Generate presigned URLs and ask the sidecar to convert to PDF.
    #  4. From here, file_bytes refers to the converted PDF; downstream
    #     parsing/extraction/KB sync flow stays unchanged.
    #  5. Source content hash anchors on the original .pptx bytes
    #     because LibreOffice output is non-deterministic.
    pptx_original_key: Optional[str] = None
    pptx_slide_count: Optional[int] = None
    pptx_slide_metadata = None
    pptx_source_hash: Optional[str] = None

    if is_pptx:
        from app.services.pptx_converter import (
            ConversionError,
            ConversionTimeout,
            ConversionUpstreamError,
            convert_pptx_to_pdf,
            parse_pptx_native,
        )

        # Stash original .pptx in S3 first.
        pptx_original_key = f"documents/{document_id}/original.pptx"
        doc_store.upload_document(
            file_bytes,
            pptx_original_key,
            content_type=(
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
        )
        pptx_source_hash = hashlib.sha256(file_bytes).hexdigest()

        # Native slide metadata pass — runs in this container.
        try:
            pptx_slide_metadata = parse_pptx_native(file_bytes)
            pptx_slide_count = pptx_slide_metadata.slide_count
        except Exception as exc:
            logger.warning(f"python-pptx parse failed: {exc}")
            raise HTTPException(
                status_code=422,
                detail=(
                    f".pptx parse failed (corrupt or password-protected): {exc}"
                ),
            )
        if pptx_slide_count == 0:
            raise HTTPException(
                status_code=422, detail=".pptx contains zero slides."
            )

        # Sidecar conversion via presigned URLs. The bytes never
        # cross the API↔sidecar HTTP boundary — they transit S3.
        target_pdf_key = f"documents/{document_id}/document.pdf"
        try:
            source_url = doc_store.presigned_get_url(pptx_original_key, expires=300)
            target_url = doc_store.presigned_put_url(
                target_pdf_key,
                expires=300,
                content_type="application/pdf",
            )
        except AttributeError:
            raise HTTPException(
                status_code=500,
                detail=(
                    "DocumentStore lacks presigned_get_url/presigned_put_url; "
                    "cannot proceed with .pptx conversion."
                ),
            )
        try:
            page_count = convert_pptx_to_pdf(
                source_url=source_url,
                target_url=target_url,
                max_bytes=PPTX_MAX_BYTES,
            )
        except ConversionTimeout as exc:
            raise HTTPException(status_code=504, detail=str(exc))
        except ConversionError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except ConversionUpstreamError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        # Slide-to-page alignment guard. LibreOffice export options
        # in the sidecar force one slide per page, but verify
        # explicitly so a future LibreOffice version change can't
        # silently misalign slide_metadata indices to PDF pages.
        if page_count and page_count != pptx_slide_count:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Slide↔page mismatch: pptx has {pptx_slide_count} slides "
                    f"but converted PDF has {page_count} pages. Re-export the "
                    "deck and try again."
                ),
            )

        # Pull the converted PDF back so downstream parse uses it.
        try:
            file_bytes = doc_store.download_document(target_pdf_key)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Could not read converted PDF from S3: {exc}",
            )

        # Rasterise each slide to a PNG and persist
        # alongside the converted PDF. The per-slide extraction +
        # validation paths reference these via SlideStatus.image_s3_key
        # at documents/{id}/slides/{N}.png.
        #
        # 2026-05-28 — moved from synchronous to BackgroundTask. On
        # 50-slide decks the synchronous call took ~30s and pushed
        # total upload latency over CloudFront's 60s origin_read_timeout
        # cap (504 to user despite backend completing fine). The
        # rasterised PNGs aren't needed until per-slide extraction or
        # validation runs — both happen later via separate routes —
        # so deferring is safe. Failures stay non-fatal: PDF flow
        # works without slide PNGs.
        from app.services.pptx_converter import rasterise_slides_to_s3

        def _rasterise_slides_bg(pptx_bytes: bytes, doc_id: str) -> None:
            try:
                rasterise_slides_to_s3(pptx_bytes, doc_id, doc_store)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"slide rasterisation failed for {doc_id}: {exc}; "
                    "per-slide extraction will run text-only"
                )

        background_tasks.add_task(
            _rasterise_slides_bg, file_bytes, document_id
        )

        # Display filename swap so the rest of the route persists
        # the canonical PDF in the right place. The original is
        # already at documents/{id}/original.pptx and tracked via
        # source_format/original_s3_key on the Document.
        pptx_display_name = file.filename
        file.filename = (file.filename or "deck").rsplit(".", 1)[0] + ".pdf"

    s3_key = f"documents/{document_id}/{file.filename}"

    # Upload to S3 — for PPTX, the converted PDF is already at
    # documents/{id}/document.pdf via the sidecar's PUT, but we also
    # write to the canonical s3_key path so the existing
    # download_document(doc.s3_key) flow doesn't need a special case.
    doc_store.upload_document(file_bytes, s3_key)

    # PPTX-source fields. For native PDF uploads these
    # all default to PDF / None and the existing flow is unchanged.
    from app.models.document import (
        SlideExtractionStatus,
        SlideStatus,
        SourceFormat,
    )

    source_format = SourceFormat.PPTX if is_pptx else SourceFormat.PDF
    slides_init: Optional[list[SlideStatus]] = None
    if is_pptx and pptx_slide_metadata is not None:
        slides_init = [
            SlideStatus(
                slide_index=s.slide_index,
                slide_type=s.slide_type,
                status=SlideExtractionStatus.PENDING,
                image_s3_key=(
                    f"documents/{document_id}/slides/{s.slide_index}.png"
                ),
            )
            for s in pptx_slide_metadata.slides
        ]

    # Create document record. For PPTX uploads, persist the
    # original .pptx display name so the UI shows the file the user
    # actually uploaded — not the post-conversion .pdf rewrite that
    # `file.filename` was mutated to above for the canonical S3 key.
    persisted_filename = pptx_display_name if is_pptx else file.filename
    document = Document(
        document_id=document_id,
        filename=persisted_filename,
        s3_key=s3_key,
        content_hash=content_hash,
        status=DocumentStatus.UPLOADED,
        document_type=doc_type,
        uploaded_at=datetime.utcnow(),
        uploaded_by=str((claims or {}).get("email", "") or "") or None,
        file_size_bytes=len(file_bytes),
        extraction_notes=notes,
        hosting_scope=scope_norm,
        source_format=source_format,
        original_s3_key=pptx_original_key,
        source_content_hash=pptx_source_hash,
        slide_count=pptx_slide_count,
        slides=slides_init,
    )
    val_store.save_document(document)
    # Audit — who uploaded what (best-effort; never breaks the upload).
    try:
        from app.services.audit_store import audit_store
        audit_store.record_from_claims(
            claims, action="document.upload", entity_type="document",
            entity_id=document_id,
            detail={"filename": persisted_filename,
                    "document_type": doc_type.value if hasattr(doc_type, "value") else str(doc_type)},
        )
    except Exception:  # noqa: BLE001
        logger.exception("audit record failed for document.upload (non-fatal)")
    response.status_code = 201

    if is_image:
        # For images: store as a single section with base64 for Vision analysis
        import base64
        from app.models.document import ParsedSection
        img_b64 = base64.b64encode(file_bytes).decode("utf-8")
        document.sections = [
            ParsedSection(
                section_id="img-1",
                title=file.filename,
                content=f"[Image file: {file.filename}]",
                page_number=1,
                level=1,
                images=[img_b64],
            )
        ]
        document.page_count = 1
        document.status = DocumentStatus.PARSED
        val_store.save_document(document)
        # Save parsed content to S3
        doc_store.save_parsed_content(document_id, document.sections, document.components)
        # Auto-trigger extraction for standards images
        if doc_type == DocumentType.STANDARDS:
            background_tasks.add_task(_trigger_extraction, document_id, file.filename)
    else:
        # PDF/PPTX-derived SDD: the pipeline is the SINGLE owner of the
        # parse → status → S3-persist → KB-sync flow. It writes
        # status="parsed", persists sections to S3, and (for standards
        # docs) triggers extraction. The old _process_document ran a
        # SECOND concurrent parse that raced the pipeline on
        # document.status — the source of the "status: parsing" vs
        # "parsed" mismatch — and its only unique output (component
        # extraction) had no downstream consumers. Removed.
        background_tasks.add_task(
            _submit_to_pipeline,
            document_id,
            file.filename,
            file_bytes,
            doc_type == DocumentType.STANDARDS,
        )

    logger.info(f"Document uploaded: {document_id} ({file.filename}, {len(file_bytes)} bytes, type={doc_type.value})")

    return DocumentUploadResponse(
        document_id=document_id,
        filename=file.filename,
        status=DocumentStatus.UPLOADED,
        message="Document uploaded successfully. Processing started in background.",
    )


@router.get("")
async def list_documents(
    document_type: Optional[str] = None,
    store: ValidationStore = Depends(get_validation_store),
):
    """List all uploaded documents.

    Args:
        document_type: Optional filter by document type ('sdd' or 'standards')
    """
    documents = store.list_documents(document_type=document_type)
    return {
        "documents": [
            {
                "document_id": doc.document_id,
                "filename": doc.filename,
                "status": doc.status.value if hasattr(doc.status, 'value') else str(doc.status),
                "document_type": doc.document_type.value if hasattr(doc.document_type, 'value') else str(doc.document_type),
                "uploaded_at": doc.uploaded_at.isoformat() if hasattr(doc.uploaded_at, 'isoformat') else str(doc.uploaded_at),
                "uploaded_by": getattr(doc, "uploaded_by", None),
                "file_size_bytes": doc.file_size_bytes,
                "page_count": doc.page_count,
                "component_count": len(doc.components) if doc.components else 0,
                "declared_tier": getattr(doc, "declared_tier", None),
                "tier_source": getattr(doc, "tier_source", None),
                "hosting_scope": getattr(doc, "hosting_scope", None),
                # Surface PPTX origin so the UI can render
                # the "PowerPoint" badge and slide-count tooltip on
                # the document card. Defaults are wire-safe ("pdf"
                # / null) when the field isn't set, so older rows
                # render without changes.
                "source_format": (
                    doc.source_format.value
                    if hasattr(getattr(doc, "source_format", None), "value")
                    else getattr(doc, "source_format", None)
                ),
                "slide_count": getattr(doc, "slide_count", None),
            }
            for doc in documents
        ],
        "total": len(documents),
    }


@router.get("/pipeline/status")
async def get_pipeline_status():
    """Get the current processing pipeline status."""
    return pipeline.get_queue_status()


@router.get("/{document_id}")
async def get_document(
    document_id: str,
    store: ValidationStore = Depends(get_validation_store),
):
    """Get a document by ID with full details."""
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    return document.model_dump(mode="json")


class TierOverrideRequest(BaseModel):
    """Body for the user-driven tier override endpoint.

    `tier=null` clears the override and reverts to the auto-detected
    value (if any). Otherwise it must be 1..5.
    """
    tier: Optional[int] = Field(default=None, ge=1, le=5)


@router.patch("/{document_id}/tier")
async def override_document_tier(
    document_id: str,
    body: TierOverrideRequest,
    store: ValidationStore = Depends(get_validation_store),
    # The declared tier decides which rules a document is validated against, so
    # changing it changes the verdict. Same floor as running a validation.
    claims: dict = Depends(require_role(Role.VALIDATOR)),
):
    """Manually set or clear the SDD's declared tier.

    Used when the auto-classifier got it wrong, or when an SDD
    pre-dates tier awareness. tier_source is stamped 'user' so the
    UI can show that the value is human-authored. Setting tier=null
    clears both fields and re-enables auto-detection on the next
    parse cycle.
    """
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    # The tier decides which rules this SDD is judged against, so retiering
    # someone else's document silently changes THEIR verdict. Holding the
    # validator role is not enough — you must own the document (or be admin).
    require_owner_or_admin(
        claims, getattr(document, "uploaded_by", None), what="document"
    )

    document.declared_tier = body.tier
    document.tier_source = "user" if body.tier is not None else None
    store.save_document(document)
    logger.info(
        f"Tier override on {document_id}: tier={body.tier} (source=user)"
    )
    return {
        "document_id": document_id,
        "declared_tier": document.declared_tier,
        "tier_source": document.tier_source,
    }


@router.get("/{document_id}/pdf")
async def get_document_file(
    document_id: str,
    store: ValidationStore = Depends(get_validation_store),
    doc_store: DocumentStore = Depends(get_document_store),
):
    """Serve the original document file (PDF or image) for viewing."""
    from fastapi.responses import Response

    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    MIME_TYPES = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tiff": "image/tiff",
        ".bmp": "image/bmp",
    }
    # For PPTX uploads, document.filename is the original
    # ".pptx" (we preserve it for display), but document.s3_key
    # points at the LibreOffice-converted ".pdf" that the iframe
    # actually renders. Serve as application/pdf so the browser
    # treats it as inline-renderable, otherwise the iframe falls
    # back to a download prompt.
    is_pptx_source = (
        getattr(document, "source_format", None) == "pptx"
        or (getattr(document.source_format, "value", "") if hasattr(document, "source_format") else "") == "pptx"
    )
    if is_pptx_source:
        media_type = "application/pdf"
    else:
        ext = "." + document.filename.rsplit(".", 1)[-1].lower() if "." in document.filename else ""
        media_type = MIME_TYPES.get(ext, "application/octet-stream")

    try:
        file_bytes = doc_store.download_document(document.s3_key)
        if not file_bytes:
            raise HTTPException(status_code=404, detail="File not found in storage")
        return Response(
            content=file_bytes,
            media_type=media_type,
            headers={"Content-Disposition": f"inline; filename=\"{document.filename}\""},
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{document_id}/slides/{slide_index}")
async def get_document_slide(
    document_id: str,
    slide_index: int,
    store: ValidationStore = Depends(get_validation_store),
    doc_store: DocumentStore = Depends(get_document_store),
):
    """Serve a rasterised slide PNG (1-based index).

    Used by the document-detail Slide Strip on the UI to render a
    visual outline of the deck. Slides live at
    documents/{id}/slides/{N}.png.
    """
    from fastapi.responses import Response

    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    if getattr(document, "source_format", None) != "pptx" and (
        getattr(document.source_format, "value", "") if hasattr(document, "source_format") else ""
    ) != "pptx":
        raise HTTPException(status_code=404, detail="Document is not a PPTX upload")

    s3_key = f"documents/{document_id}/slides/{slide_index}.png"
    file_bytes = doc_store.download_document(s3_key)
    if not file_bytes:
        raise HTTPException(status_code=404, detail=f"Slide {slide_index} not found in storage")

    return Response(
        content=file_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/{document_id}/sync-status")
async def get_document_sync_status(
    document_id: str,
    store: ValidationStore = Depends(get_validation_store),
):
    """Get the sync/pipeline status for a specific document."""
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    # Check pipeline for active job info
    job = pipeline.get_job_status(document_id)

    return {
        "document_id": document_id,
        "filename": document.filename,
        "sync_status": document.sync_status,
        "processing_step": document.processing_step,
        "sync_error": document.sync_error,
        "queued_at": document.queued_at.isoformat() if document.queued_at else None,
        "sync_started_at": document.sync_started_at.isoformat() if document.sync_started_at else None,
        "sync_completed_at": document.sync_completed_at.isoformat() if document.sync_completed_at else None,
        "pipeline_stage": job.stage.value if job else None,
    }


async def _submit_to_pipeline(
    document_id: str,
    filename: str,
    pdf_bytes: bytes,
    is_standards_doc: bool = False,
):
    """Submit a document to the processing pipeline."""
    try:
        await pipeline.submit(
            document_id=document_id,
            filename=filename,
            pdf_bytes=pdf_bytes,
            is_standards_doc=is_standards_doc,
        )
    except Exception as e:
        logger.error(f"Failed to submit document {document_id} to pipeline: {e}")


@router.get("/{document_id}/diagrams")
async def get_document_diagrams(
    document_id: str,
    store: ValidationStore = Depends(get_validation_store),
):
    """Get extracted diagrams from a parsed document."""
    document = store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    diagrams = []
    if document.sections:
        for section in document.sections:
            if hasattr(section, 'images') and section.images:
                for img_idx, img_b64 in enumerate(section.images):
                    diagrams.append({
                        "image_url": f"data:image/png;base64,{img_b64}",
                        "page_number": section.page_number if hasattr(section, 'page_number') else 0,
                        "caption": f"Architecture diagram from {section.title}" if hasattr(section, 'title') else f"Diagram {img_idx + 1}",
                    })

    return {"diagrams": diagrams, "total": len(diagrams)}
