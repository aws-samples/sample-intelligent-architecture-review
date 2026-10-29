# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Validation routes - parallel per-standard validation in-process.

Validation runs as a FastAPI BackgroundTask: the endpoint returns 202
immediately, the parallel_validator service fans out one Bedrock call
per standard (~30s wall-clock total), and the frontend polls for the
result.
"""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response

from app.api.dependencies import get_validation_store
from app.api.rbac import Role, actor_email, require_owner_or_admin, require_role
from app.models.validation import (
    ComplianceStatus,
    ValidationRequest,
    ValidationResult,
    ValidationStatus,
    ValidationSummary,
)
from app.services.parallel_validator import (
    validate_document,
    ValidationCancelled,
    request_cancel,
    clear_cancel,
)
from app.services.validation_store import ValidationStore
from app.bedrock_models import SONNET, model_for

logger = logging.getLogger(__name__)

router = APIRouter()


async def _run_validation(
    document_id: str,
    validation_id: str,
    standards_set: str,
    document_content_hash: str,
    standards_set_hash: str,
    store: ValidationStore,
    standard_ids: Optional[list[str]] = None,
    transient_standards: Optional[list[dict]] = None,
    # Tier override from the request. None means "use the document's
    # declared_tier".
    tier_override: Optional[int] = None,
) -> None:
    """Background task: run the parallel validator and persist results.

    Always writes a final ValidationResult — completed on success, failed
    with an error message on exception — so the frontend never sees a
    permanently-pending row.
    """
    started_at = datetime.utcnow()
    try:
        document = store.get_document(document_id)
        if not document:
            raise ValueError(f"Document {document_id} not found")

        standards = store.list_standards()
        if standard_ids:
            wanted = set(standard_ids)
            standards = [s for s in standards if s.get("id") in wanted]
        if transient_standards:
            standards = list(standards) + list(transient_standards)

        # Update the existing PENDING row with the total count so the
        # frontend can render a progress bar from the very first poll.
        pending = store.get_validation(validation_id)
        if pending:
            pending.total_standards = len(standards)
            store.save_validation(pending)

        # Cheap, throttled live-progress writer. We update DynamoDB at
        # roughly every 5 completions to keep the frontend's "X / Y
        # evaluated" display moving without flooding writes.
        async def _on_progress(evaluated: int, total: int) -> None:
            if evaluated == total or evaluated % 5 == 0:
                try:
                    snapshot = ValidationResult(
                        validation_id=validation_id,
                        document_id=document_id,
                        status=ValidationStatus.PENDING,
                        standards_set=standards_set,
                        started_at=started_at,
                        document_content_hash=document_content_hash,
                        standards_set_hash=standards_set_hash,
                        evaluated_count=evaluated,
                        total_standards=total,
                    )
                    await asyncio.to_thread(store.save_validation, snapshot)
                except Exception:
                    logger.exception(f"Progress update failed for {validation_id}")

        result = await validate_document(
            document,
            standards,
            document_content_hash=document_content_hash,
            standards_set_hash=standards_set_hash,
            standards_set=standards_set,
            validation_id=validation_id,
            on_progress=_on_progress,
            tier_override=tier_override,
        )
        store.save_validation(result)
    except ValidationCancelled:
        # User cancelled — persist CANCELLED (terminal), not FAILED.
        logger.info(f"Validation {validation_id} cancelled; persisting CANCELLED")
        try:
            store.save_validation(
                ValidationResult(
                    validation_id=validation_id,
                    document_id=document_id,
                    status=ValidationStatus.CANCELLED,
                    standards_set=standards_set,
                    started_at=started_at,
                    completed_at=datetime.utcnow(),
                    error_message="Cancelled by user.",
                    document_content_hash=document_content_hash,
                    standards_set_hash=standards_set_hash,
                )
            )
        except Exception:
            logger.exception(f"Failed to persist CANCELLED status for {validation_id}")
    except Exception as e:
        logger.exception(f"Validation {validation_id} failed")
        try:
            store.save_validation(
                ValidationResult(
                    validation_id=validation_id,
                    document_id=document_id,
                    status=ValidationStatus.FAILED,
                    standards_set=standards_set,
                    started_at=started_at,
                    completed_at=datetime.utcnow(),
                    error_message=str(e)[:1000],
                    document_content_hash=document_content_hash,
                    standards_set_hash=standards_set_hash,
                )
            )
        except Exception:
            logger.exception(f"Failed to persist FAILED status for {validation_id}")
    finally:
        # Drop the cancel flag so the in-process registry doesn't grow.
        clear_cancel(validation_id)


_VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}


def _normalize_transient_standards(raw_list: list[dict]) -> list[dict]:
    """Validate inline transient rules and stamp them with stable temp ids.

    Each entry must carry name, description, severity, category, and
    validation_criteria. Severity is clamped to the valid enum and
    category defaults to "general". Temp ids are derived from a SHA256
    of the canonical fields so re-running with the same payload hits
    the cache instead of producing a sibling validation.
    """
    normalized: list[dict] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        description = str(raw.get("description", "")).strip()
        criteria = str(raw.get("validation_criteria", "")).strip()
        if not (name and description and criteria):
            raise HTTPException(
                status_code=400,
                detail=(
                    "transient_standards entries must include name, "
                    "description, and validation_criteria."
                ),
            )
        severity = str(raw.get("severity", "medium")).strip().lower()
        if severity not in _VALID_SEVERITIES:
            severity = "medium"
        category = str(raw.get("category", "general")).strip().lower() or "general"

        canonical = json.dumps(
            {
                "name": name,
                "description": description,
                "severity": severity,
                "category": category,
                "validation_criteria": criteria,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8].upper()
        normalized.append(
            {
                "id": f"HA-T-{digest}",
                "name": name,
                "description": description,
                "severity": severity,
                "category": category,
                "validation_criteria": criteria,
                # Marker the agent / UI can use to know this rule was transient.
                "source": "transient",
            }
        )
    return normalized


def _ensure_document_content_hash(document, store: ValidationStore) -> str:
    """Return document.content_hash, computing/backfilling from S3 if missing."""
    if getattr(document, "content_hash", None):
        return document.content_hash

    # Uploaded before content hashes were stored — fetch bytes from S3
    # and backfill
    from app.services.document_store import document_store as doc_store
    file_bytes = doc_store.download_document(document.s3_key)
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    document.content_hash = content_hash
    store.save_document(document)
    logger.info(f"Backfilled content_hash for legacy document {document.document_id}")
    return content_hash


@router.post("", response_model=dict)
async def create_validation(
    response: Response,
    request: ValidationRequest,
    background_tasks: BackgroundTasks,
    store: ValidationStore = Depends(get_validation_store),
    claims: dict = Depends(require_role(Role.VALIDATOR)),
):
    """
    Trigger a new validation for a document.

    Validation runs in a FastAPI BackgroundTask using the parallel
    per-standard validator. Wall-clock target is ~30 seconds for ~80
    standards. The endpoint returns 202 immediately with a validation_id;
    the frontend polls GET /api/validations/{id} until status becomes
    `completed` or `failed`.

    Idempotent: when force=False (default) and a prior validation exists for
    the same (document_content_hash, standards_set_hash), the existing
    validation_id is returned without re-running.
    """
    # Verify document exists
    document = store.get_document(request.document_id)
    if not document:
        raise HTTPException(
            status_code=404,
            detail=f"Document {request.document_id} not found",
        )

    # Check document is parsed
    doc_status = document.status.value if hasattr(document.status, "value") else str(document.status)
    if doc_status not in ("parsed", "analyzed"):
        raise HTTPException(
            status_code=400,
            detail=f"Document must be parsed before validation. Current status: {doc_status}",
        )

    # Soft page cap. The chunked validator can in principle process
    # arbitrary lengths, but Bedrock cost grows linearly with chunk
    # count (one call per (rule, chunk)). Refuse runaway docs at the
    # API boundary so a 1000-page upload doesn't quietly burn $50.
    max_pages = int(os.getenv("VALIDATION_MAX_PAGES", "250"))
    page_count = getattr(document, "page_count", None) or 0
    if page_count > max_pages:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Document has {page_count} pages, exceeding the validation cap of {max_pages}. "
                f"Split the document or contact the administrator to raise the limit."
            ),
        )

    # Check that at least one standard exists
    standards = store.list_standards()
    if not standards:
        raise HTTPException(
            status_code=400,
            detail="No validation standards configured. Please upload a standards document or add rules manually before running validation.",
        )

    # Validate the optional subset and shrink the in-memory list. If an ID
    # in the request doesn't match anything we have, fail fast — silently
    # producing 0 findings would be confusing.
    selected_standard_ids: Optional[list[str]] = None
    if request.standard_ids:
        available_ids = {s.get("id") for s in standards}
        unknown = [sid for sid in request.standard_ids if sid not in available_ids]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown standard IDs: {unknown}",
            )
        selected_standard_ids = list(request.standard_ids)
        if not selected_standard_ids:
            raise HTTPException(
                status_code=400,
                detail="standard_ids was provided but is empty.",
            )

    # Validate inline transient rules. Reject empty/malformed entries
    # rather than letting them silently produce zero findings.
    transient_standards = (
        _normalize_transient_standards(request.transient_standards)
        if request.transient_standards
        else None
    )
    if transient_standards is not None and not transient_standards:
        # User passed [] explicitly — let it slide as no transients.
        transient_standards = None

    if not selected_standard_ids and not transient_standards and not standards:
        # Nothing to validate at all
        raise HTTPException(
            status_code=400,
            detail="No standards selected for validation.",
        )

    # Compute the cache key. When a subset is selected, the hash covers
    # only those rules; transients fold into the same projection so two
    # runs with different inline rules don't collide in the cache.
    document_content_hash = _ensure_document_content_hash(document, store)
    standards_set_hash = store.standards_set_hash(
        selected_ids=selected_standard_ids,
        transient_standards=transient_standards,
    )

    # Cache lookup — short-circuit when an identical validation already exists
    if not request.force:
        cached = store.find_cached_validation(
            request.document_id, document_content_hash, standards_set_hash
        )
        if cached:
            cached_status = (
                cached.status.value if hasattr(cached.status, "value") else str(cached.status)
            )
            logger.info(
                f"Validation cache hit: returning {cached.validation_id} "
                f"(status={cached_status}) for document {request.document_id}"
            )
            response.status_code = 200
            return {
                "validation_id": cached.validation_id,
                "document_id": cached.document_id,
                "status": cached_status,
                "cached": True,
                "message": "Reusing prior validation result (pass force=true to re-run)",
            }

    # Create validation record (status: pending) with cache keys persisted
    validation_id = f"val-{uuid.uuid4().hex[:12]}"
    validation = ValidationResult(
        validation_id=validation_id,
        document_id=request.document_id,
        status=ValidationStatus.PENDING,
        standards_set=request.standards_set,
        started_at=datetime.utcnow(),
        document_content_hash=document_content_hash,
        standards_set_hash=standards_set_hash,
        # Records who owns this run so only they (or an admin) can cancel it.
        triggered_by=actor_email(claims) or None,
    )
    store.save_validation(validation)

    # Run the parallel validator as a FastAPI BackgroundTask. It updates
    # the same validation_id row (PENDING → COMPLETED/FAILED) in DynamoDB.
    background_tasks.add_task(
        _run_validation,
        request.document_id,
        validation_id,
        request.standards_set,
        document_content_hash,
        standards_set_hash,
        store,
        selected_standard_ids,
        transient_standards,
        # Pass the optional tier override through to the
        # parallel validator. None preserves declared-tier behavior.
        request.tier_override,
    )
    selected_count = (
        len(selected_standard_ids) if selected_standard_ids else len(standards)
    )
    transient_count = len(transient_standards) if transient_standards else 0
    logger.info(
        f"Validation {validation_id} queued for document {request.document_id} "
        f"({selected_count} library + {transient_count} transient of {len(standards)} standards)"
    )

    # Audit — who triggered this validation (best-effort; never blocks).
    try:
        from app.services.audit_store import audit_store
        audit_store.record_from_claims(
            claims, action="validation.create", entity_type="validation",
            entity_id=validation_id,
            detail={"document_id": request.document_id, "forced": bool(request.force)},
        )
    except Exception:  # noqa: BLE001
        logger.exception("audit record failed for validation.create (non-fatal)")

    response.status_code = 202
    return {
        "validation_id": validation_id,
        "document_id": request.document_id,
        "status": ValidationStatus.PENDING.value,
        "cached": False,
        "message": "Validation queued. Check status with GET /validations/{validation_id}",
    }


@router.post("/{validation_id}/cancel")
async def cancel_validation(
    validation_id: str,
    store: ValidationStore = Depends(get_validation_store),
    # Declared as a parameter rather than in `dependencies=[...]` because the
    # handler needs the resolved claims for the ownership check below.
    claims: dict = Depends(require_role(Role.VALIDATOR)),
):
    """Request cancellation of an in-flight validation.

    Sets the cooperative cancel flag; the running validator checks it
    between rule batches and stops with status=CANCELLED. Idempotent and
    safe to call on an already-terminal run (returns its current status).

    Restricted to whoever started the run (or an admin): holding the validator
    role means you may run validations, not that you may kill someone else's
    half-finished one.
    """
    validation = store.get_validation(validation_id)
    if not validation:
        raise HTTPException(
            status_code=404, detail=f"Validation {validation_id} not found"
        )
    require_owner_or_admin(claims, validation.triggered_by, what="validation")
    current = (
        validation.status.value
        if hasattr(validation.status, "value")
        else str(validation.status)
    )
    # Already terminal — nothing to cancel.
    _TERMINAL = {
        ValidationStatus.COMPLETED.value,
        ValidationStatus.FAILED.value,
        ValidationStatus.CANCELLED.value,
    }
    if current in _TERMINAL:
        return {
            "validation_id": validation_id,
            "status": current,
            "cancelled": current == ValidationStatus.CANCELLED.value,
            "message": f"Validation already {current}; nothing to cancel.",
        }
    # The signal is persisted on the validation row, because this request often
    # lands on a different ECS task than the one running the job. If that write
    # fails we must NOT claim the run is stopping — the user would stop watching
    # a job that keeps billing.
    recorded = request_cancel(validation_id)
    if not recorded:
        logger.error(f"Cancel signal for {validation_id} could not be persisted")
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not record the cancellation — the run is still going. "
                "Please try again."
            ),
        )
    logger.info(f"Cancellation requested for validation {validation_id}")
    return {
        "validation_id": validation_id,
        "status": "cancelling",
        "cancelled": True,
        "message": "Cancellation requested; the run will stop at the next checkpoint.",
    }


@router.get("")
async def list_validations(
    document_id: str = None,
    store: ValidationStore = Depends(get_validation_store),
):
    """List all validations, optionally filtered by document_id."""
    validations = store.list_validations(document_id=document_id)

    summaries = []
    for v in validations:
        # Counts come off the row, not from `v.findings`: findings live in S3
        # and the list read leaves them empty, so deriving the counts here
        # reported 0 for every validation.
        summaries.append(
            ValidationSummary(
                validation_id=v.validation_id,
                document_id=v.document_id,
                status=v.status,
                standards_set=v.standards_set,
                started_at=v.started_at,
                completed_at=v.completed_at,
                overall_score=v.overall_score,
                findings_count=v.findings_count,
                critical_count=v.critical_count,
                high_count=v.high_count,
                evaluated_count=getattr(v, "evaluated_count", 0),
                total_standards=getattr(v, "total_standards", 0),
            ).model_dump(mode="json")
        )

    return {"validations": summaries, "total": len(summaries)}


@router.get("/{validation_id}")
async def get_validation(
    validation_id: str,
    store: ValidationStore = Depends(get_validation_store),
):
    """Get a validation result by ID with all findings."""
    validation = store.get_validation(validation_id)
    if not validation:
        raise HTTPException(
            status_code=404,
            detail=f"Validation {validation_id} not found",
        )

    return validation.model_dump(mode="json")


@router.get("/{validation_id}/report")
async def get_validation_report(
    validation_id: str,
    store: ValidationStore = Depends(get_validation_store),
):
    """Get a detailed validation report."""
    validation = store.get_validation(validation_id)
    if not validation:
        raise HTTPException(
            status_code=404,
            detail=f"Validation {validation_id} not found",
        )

    if validation.status != ValidationStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"Validation is not completed. Status: {validation.status.value}",
        )

    report = {
        "validation_id": validation.validation_id,
        "document_id": validation.document_id,
        "overall_score": validation.overall_score,
        "status": validation.status.value,
        "executive_summary": validation.summary or "No summary available.",
        "statistics": {
            "total_requirements": len(validation.findings),
            "passed": sum(1 for f in validation.findings if f.status == ComplianceStatus.PASS),
            "failed": sum(1 for f in validation.findings if f.status == ComplianceStatus.FAIL),
            "warnings": sum(1 for f in validation.findings if f.status == ComplianceStatus.WARNING),
        },
        "findings": [f.model_dump() for f in validation.findings],
    }
    return report


# ───────────────────────────────────────────────────────────────────────
# Downloadable report — CSV + PDF. The JSON /report above stays for
# programmatic use; these two set Content-Disposition so a browser
# downloads a file. CSV is stdlib; PDF uses reportlab (pure-Python).
# ───────────────────────────────────────────────────────────────────────

_CSV_COLUMNS = [
    "requirement_id",
    "requirement_title",
    "severity",
    "status",
    "rule_type",
    "via_rule_id",
    "affected_components",
    "source_section",
    "evidence",
    "recommendation",
]


def _require_completed(store: ValidationStore, validation_id: str):
    validation = store.get_validation(validation_id)
    if not validation:
        raise HTTPException(status_code=404, detail=f"Validation {validation_id} not found")
    if validation.status != ValidationStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"Report available only for completed validations. Status: {validation.status.value}",
        )
    return validation


@router.get("/{validation_id}/report.csv")
async def download_validation_report_csv(
    validation_id: str,
    store: ValidationStore = Depends(get_validation_store),
):
    """Download the findings as CSV (one row per finding)."""
    import csv
    import io

    validation = _require_completed(store, validation_id)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for f in validation.findings:
        d = f.model_dump()
        writer.writerow({
            "requirement_id": d.get("requirement_id", ""),
            "requirement_title": d.get("requirement_title", ""),
            "severity": _enum_str(d.get("severity")),
            "status": _enum_str(d.get("status")),
            "rule_type": d.get("rule_type") or "",
            "via_rule_id": d.get("via_rule_id") or "",
            "affected_components": "; ".join(d.get("affected_components") or []),
            "source_section": d.get("source_section") or "",
            "evidence": (d.get("evidence") or "").replace("\n", " ").strip(),
            "recommendation": (d.get("recommendation") or "").replace("\n", " ").strip(),
        })
    filename = f"validation-{validation_id}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{validation_id}/report.pdf")
async def download_validation_report_pdf(
    validation_id: str,
    store: ValidationStore = Depends(get_validation_store),
):
    """Download a formatted PDF report (summary + findings table)."""
    validation = _require_completed(store, validation_id)
    try:
        pdf_bytes = _render_validation_pdf(validation, store)
    except ImportError:
        # reportlab not installed — fail clearly rather than 500 opaquely.
        raise HTTPException(
            status_code=501,
            detail="PDF export unavailable (reportlab not installed). Use report.csv.",
        )
    filename = f"validation-{validation_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _enum_str(v) -> str:
    return v.value if hasattr(v, "value") else ("" if v is None else str(v))


def _render_validation_pdf(validation, store) -> bytes:
    """Render a validation to a PDF byte string via reportlab. Raises
    ImportError if reportlab is absent (handled by the caller → 501)."""
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )

    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=7, leading=9)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, leading=10)

    # Resolve the document filename for a human-readable header.
    doc_name = validation.document_id
    try:
        d = store.get_document(validation.document_id)
        if d and getattr(d, "filename", None):
            doc_name = d.filename
    except Exception:  # noqa: BLE001
        pass

    score = validation.overall_score
    score_str = "N/A" if score is None else f"{score}%"
    arch = getattr(validation, "architecture_verdict", None)
    arch_str = _enum_str(arch) or "—"

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
    )
    elements = [
        Paragraph("Resilience Validation Report", styles["Title"]),
        Spacer(1, 6),
        Paragraph(f"<b>Document:</b> {doc_name}", small),
        Paragraph(f"<b>Validation ID:</b> {validation.validation_id}", small),
        Paragraph(f"<b>Compliance score:</b> {score_str}", small),
        Paragraph(f"<b>Architecture verdict:</b> {arch_str}", small),
        Paragraph(f"<b>Summary:</b> {validation.summary or '—'}", small),
        Spacer(1, 12),
    ]

    # Findings table.
    header = ["ID", "Requirement", "Severity", "Status", "Evidence"]
    rows = [header]
    for f in validation.findings:
        d = f.model_dump()
        rows.append([
            Paragraph(str(d.get("requirement_id", "")), cell),
            Paragraph(str(d.get("requirement_title", "")), cell),
            Paragraph(_enum_str(d.get("severity")), cell),
            Paragraph(_enum_str(d.get("status")), cell),
            Paragraph((d.get("evidence") or "")[:400], cell),
        ])
    table = Table(rows, colWidths=[0.8 * inch, 1.7 * inch, 0.6 * inch, 0.9 * inch, 3.2 * inch], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
    ]))
    elements.append(table)
    pdf.build(elements)
    return buf.getvalue()


# ───────────────────────────────────────────────────────────────────────
# Per-finding explainer chat.
#
# Read-only: the LLM explains the existing verdict using the rule, the
# verdict, the trace (prompt + chunk + raw model response), and the
# user's follow-up. It never mutates findings — that's the whole point
# of "show your work, but don't redo your work".
# ───────────────────────────────────────────────────────────────────────


from pydantic import BaseModel, Field

import re as _re


# Verdict-flip detector. The system prompt's "do not change
# the verdict" rule is paper-thin enforcement. After the model returns,
# we run these patterns over the reply; on a match we APPEND a ⚠
# disclaimer rather than blocking the message. Auditors get a
# defensible artifact; the user still sees the model's text.
_VERDICT_FLIP_PATTERNS = [
    # Direct flip language
    _re.compile(r"\b(should|could|would)\s+(have\s+)?(passed|failed)", _re.I),
    _re.compile(r"\b(actually|really)\s+(is|was)\s+a?\s*(pass|fail|warning)", _re.I),
    _re.compile(r"\bverdict\s+(should|could|is\s+wrong)", _re.I),
    # Counterfactual hedging — "if X were enabled, this would pass" —
    # excludes rule-quoting context like "the rule says it passes if X"
    # via fixed-width lookbehind on common rule-quoting prefixes.
    _re.compile(
        r"(?<!\brule\s)(?<!\brequirement\s)(?<!\bstandard\s)"
        r"(?<!\bspec\s)(?<!\bcriteria\s)(?<!\bsays\s)(?<!\bstates\s)"
        r"(?<!\bdefinition\s)"
        r"\b(if|had|were|would\s+have)\b[^.]{0,120}\b"
        r"(pass|passed|fail|failed|satisfy|satisfied|meet|met|comply|complied)",
        _re.I,
    ),
    # Insinuation: validator overlooked / missed evidence
    _re.compile(r"\b(overlooked|missed|did\s+not\s+(see|consider))\b", _re.I),
]


def _attach_verdict_flip_warning(reply: str, stored_verdict: str) -> str:
    """If the model's reply contains verdict-flip or counterfactual
    hedging language, append a disclaimer block. Don't block the
    message — annotate it."""
    if any(p.search(reply) for p in _VERDICT_FLIP_PATTERNS):
        return (
            reply
            + f"\n\n⚠ This message contains language suggesting the verdict "
            f"could differ. The stored verdict is **{stored_verdict}**; "
            f"to challenge it, re-run validation."
        )
    return reply


def _build_chat_system_prompt(
    *,
    finding,
    rule: Optional[dict],
    document,
    validation,
) -> str:
    """Assemble the system prompt for the per-finding explainer chat.

    The prompt leads with the captured verbalized reasoning. When
    trace.reasoning is missing (older validation, thinking off, or the
    model didn't emit reasoning content), the prompt explicitly says so
    and forbids inventing reasoning. The verdict-change refusal is also
    here, plus belt-and-suspenders is the post-hoc regex detector that
    annotates suspect replies.
    """
    # Document context block.
    doc_block = ""
    if document is not None:
        doc_block = (
            f"Document: {getattr(document, 'filename', 'unknown')}\n"
            f"Document id: {getattr(document, 'document_id', 'unknown')}\n"
            f"Declared tier: {getattr(document, 'declared_tier', None) or 'unspecified'}\n"
            f"Hosting scope: {getattr(document, 'hosting_scope', None) or 'unspecified'}\n"
            f"Source format: "
            f"{getattr(getattr(document, 'source_format', None), 'value', getattr(document, 'source_format', 'unknown'))}\n"
            f"Page count: {getattr(document, 'page_count', None) or 'unknown'}\n"
        )
    if validation is not None:
        tier_used = getattr(validation, "tier_used", None)
        if tier_used:
            doc_block += f"Validated as tier: {tier_used}"
            tier_override = getattr(validation, "tier_override", None)
            declared = getattr(document, "declared_tier", None) if document else None
            if tier_override and declared and tier_override != declared:
                doc_block += f" (override; declared {declared})"
            doc_block += "\n"

    # Rule definition block.
    if rule is not None:
        rule_block = (
            f"Rule: {rule.get('id', getattr(finding, 'requirement_id', '?'))}\n"
            f"Name: {rule.get('name', getattr(finding, 'requirement_title', ''))}\n"
            f"Description: {rule.get('description', '')}\n"
            f"Validation criteria: {rule.get('validation_criteria', '')}\n"
            f"Severity: {rule.get('severity', '')}\n"
            f"Layer: {rule.get('layer', 'general')}\n"
            f"Rule type: {rule.get('rule_type') or 'founding'}\n"
        )
    else:
        rule_block = (
            f"Rule: {getattr(finding, 'requirement_id', '?')}\n"
            f"Name: {getattr(finding, 'requirement_title', '')}\n"
        )

    # Verdict block.
    status_val = (
        finding.status.value if hasattr(getattr(finding, "status", None), "value")
        else getattr(finding, "status", "")
    )
    severity_val = (
        finding.severity.value if hasattr(getattr(finding, "severity", None), "value")
        else getattr(finding, "severity", "")
    )
    verdict_block = (
        f"Verdict: {status_val}\n"
        f"Severity assigned: {severity_val}\n"
        f"Evidence the validator cited:\n{getattr(finding, 'evidence', '')}\n\n"
        f"Recommendation: {getattr(finding, 'recommendation', None) or '(none)'}\n"
    )
    via = getattr(finding, "via_rule_id", None)
    if via:
        verdict_block += f"Decision-tree link: this finding's verdict was influenced by {via}.\n"
    tried = getattr(finding, "tried_substitution_ids", None) or []
    if tried:
        verdict_block += f"Substitutions tried (none rescued): {', '.join(tried)}\n"

    # Reasoning block leads. When absent, we say so
    # explicitly and forbid the model from inventing reasoning.
    trace = getattr(finding, "trace", None)
    if trace:
        td = trace.model_dump() if hasattr(trace, "model_dump") else trace
        reasoning = td.get("reasoning") or ""
        chunk_excerpt = td.get("chunk_excerpt", "") or ""
        prompt_text = td.get("prompt", "") or ""
        raw_response = td.get("raw_response", "") or ""
        if reasoning:
            trace_block = (
                "--- Validator's verbalized reasoning at decision time ---\n"
                f"{reasoning}\n\n"
                "(This is the model's chain of thought captured at "
                "validation time. Generally aligned with the verdict but "
                "not a faithful audit trail of internal decision logic. "
                "When the user asks why or how, quote and paraphrase "
                "from this block. You may NOT invent reasoning steps "
                "that aren't here. If the user asks something this "
                "block doesn't cover, say 'the captured reasoning "
                "doesn't address that.')\n\n"
                "--- Validator's final answer ---\n"
                f"{raw_response}\n\n"
                "--- Document text the validator saw ---\n"
                f"{chunk_excerpt}\n\n"
                "--- Prompt sent to the validator ---\n"
                f"{prompt_text}\n"
            )
        else:
            trace_block = (
                "Verbalized reasoning: NOT CAPTURED for this finding. "
                "Reasoning capture may have been disabled at runtime, "
                "or the model returned no reasoning content. Do not "
                "invent reasoning. You can still describe the verdict "
                "and quote the raw response.\n\n"
                "--- Validator's final answer ---\n"
                f"{raw_response}\n\n"
                "--- Document text the validator saw ---\n"
                f"{chunk_excerpt}\n\n"
                "--- Prompt sent to the validator ---\n"
                f"{prompt_text}\n"
            )
    else:
        trace_block = (
            "Original validator trace: NOT AVAILABLE for this finding. "
            "If the user asks about prompts, chunks, or the model's "
            "reasoning, you must say 'the trace was not captured for "
            "this finding' instead of speculating. You can still "
            "explain what the verdict means and why the rule matters "
            "based on the rule definition above.\n"
        )

    return (
        "You are an HA validation explainer. The user is reading a single "
        "finding from a System Design Document validation and wants to "
        "understand HOW the validator reached its verdict. Be concise "
        "(≤ 200 words unless the user asks for more), grounded in the "
        "captured trace, and willing to say 'the trace doesn't show "
        "that' when the user asks something the trace can't answer. "
        "You may NOT change the verdict or imply it should have been "
        "different. The verdict was committed at validation time; this "
        "conversation is read-only. If the user wants a different "
        "verdict, the answer is 're-run validation with force=true'.\n\n"
        f"{doc_block}\n{rule_block}\n{verdict_block}\n{trace_block}"
    )


class FindingChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class FindingChatRequest(BaseModel):
    messages: list[FindingChatMessage] = Field(..., min_length=1)


class FindingChatResponse(BaseModel):
    reply: str


@router.post(
    "/{validation_id}/findings/{requirement_id}/chat",
    response_model=FindingChatResponse,
    dependencies=[Depends(require_role(Role.VALIDATOR))],
)
async def chat_about_finding(
    validation_id: str,
    requirement_id: str,
    request: FindingChatRequest,
    store: ValidationStore = Depends(get_validation_store),
):
    """Read-only conversational explainer for a single finding.

    The frontend sends a thread of user/assistant messages; we prepend
    a system message that hands the model the rule + verdict + trace
    context so it can reason about how the verdict was reached. The
    reply is returned synchronously — explanations are short, no need
    for the async-job machinery the curator chat uses.
    """
    if not request.messages or request.messages[-1].role != "user":
        raise HTTPException(
            status_code=400,
            detail="Last message must be from the user.",
        )

    validation = store.get_validation(validation_id)
    if not validation:
        raise HTTPException(status_code=404, detail=f"Validation {validation_id} not found")

    finding = next(
        (f for f in validation.findings if f.requirement_id == requirement_id),
        None,
    )
    if finding is None:
        raise HTTPException(
            status_code=404,
            detail=f"Finding {requirement_id} not in validation {validation_id}",
        )

    # System prompt assembly extracted into _build_chat_system_prompt
    # so it's individually testable. Reasoning leads, verdict-change is
    # forbidden, missing-reasoning is explicitly disclosed.
    document = store.get_document(validation.document_id)
    rule = store.get_standard(requirement_id)
    system_prompt = _build_chat_system_prompt(
        finding=finding,
        rule=rule,
        document=document,
        validation=validation,
    )

    # Bedrock Converse — synchronous, single short turn. Reuse the
    # validator's client factory for connection pooling.
    from app.services.parallel_validator import _make_bedrock_client
    client = _make_bedrock_client()
    model_id = model_for("BEDROCK_MODEL_ID", SONNET)

    bedrock_messages = [
        {"role": m.role, "content": [{"text": m.content}]}
        for m in request.messages
    ]

    try:
        resp = await asyncio.to_thread(
            client.converse,
            modelId=model_id,
            system=[{"text": system_prompt}],
            messages=bedrock_messages,
            inferenceConfig={"maxTokens": 800, "temperature": 0.2},
        )
    except Exception as e:
        logger.exception(f"finding chat for {validation_id}/{requirement_id} failed")
        raise HTTPException(status_code=502, detail=f"Bedrock call failed: {e}")

    # Extract text content from the converse response.
    out_blocks = (
        resp.get("output", {}).get("message", {}).get("content", [])
    )
    reply = "".join(b.get("text", "") for b in out_blocks if "text" in b).strip()
    if not reply:
        reply = "(no reply — model returned an empty response)"

    # Output-side verdict-flip detector. If the model's
    # reply contains language suggesting the verdict could differ
    # (direct flip, counterfactual hedging, or "validator overlooked X"
    # insinuation), append a ⚠ disclaimer block. Don't block the
    # message — annotate it.
    stored_verdict = (
        finding.status.value if hasattr(finding.status, "value") else str(finding.status)
    )
    reply = _attach_verdict_flip_warning(reply, stored_verdict)

    return FindingChatResponse(reply=reply)


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Events frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/{validation_id}/findings/{requirement_id}/chat/stream",
             dependencies=[Depends(require_role(Role.VALIDATOR))])
async def chat_about_finding_stream(
    validation_id: str,
    requirement_id: str,
    request: FindingChatRequest,
    store: ValidationStore = Depends(get_validation_store),
):
    """Streaming (SSE) variant of the per-finding explainer chat.

    Emits `event: delta` frames as the model generates tokens, a final
    `event: warning` frame if the verdict-flip detector fires on the
    complete reply, then `event: done`. Errors surface as `event: error`.

    Frames use `Cache-Control: no-transform` so CloudFront (which has
    compress=true on /api/*) does not buffer the response to gzip it,
    which would defeat token-by-token streaming.
    """
    from fastapi.responses import StreamingResponse

    if not request.messages or request.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="Last message must be from the user.")

    validation = store.get_validation(validation_id)
    if not validation:
        raise HTTPException(status_code=404, detail=f"Validation {validation_id} not found")

    finding = next(
        (f for f in validation.findings if f.requirement_id == requirement_id),
        None,
    )
    if finding is None:
        raise HTTPException(
            status_code=404,
            detail=f"Finding {requirement_id} not in validation {validation_id}",
        )

    document = store.get_document(validation.document_id)
    rule = store.get_standard(requirement_id)
    system_prompt = _build_chat_system_prompt(
        finding=finding, rule=rule, document=document, validation=validation,
    )
    stored_verdict = (
        finding.status.value if hasattr(finding.status, "value") else str(finding.status)
    )

    from app.services.parallel_validator import _make_bedrock_client
    client = _make_bedrock_client()
    model_id = model_for("BEDROCK_MODEL_ID", SONNET)
    bedrock_messages = [
        {"role": m.role, "content": [{"text": m.content}]} for m in request.messages
    ]

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _pump():
            """Run the blocking botocore EventStream off the event loop,
            forwarding text deltas onto the asyncio queue."""
            try:
                resp = client.converse_stream(
                    modelId=model_id,
                    system=[{"text": system_prompt}],
                    messages=bedrock_messages,
                    inferenceConfig={"maxTokens": 800, "temperature": 0.2},
                )
                for ev in resp["stream"]:
                    block = ev.get("contentBlockDelta")
                    if block:
                        text = block.get("delta", {}).get("text", "")
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, ("delta", text))
                loop.call_soon_threadsafe(queue.put_nowait, ("eos", None))
            except Exception as exc:  # noqa: BLE001 — surfaced as an SSE error frame
                logger.exception(
                    f"finding chat stream for {validation_id}/{requirement_id} failed"
                )
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

        loop.run_in_executor(None, _pump)

        parts: list[str] = []
        while True:
            kind, payload = await queue.get()
            if kind == "delta":
                parts.append(payload)
                yield _sse("delta", {"text": payload})
            elif kind == "error":
                yield _sse("error", {"detail": f"Bedrock call failed: {payload}"})
                return
            elif kind == "eos":
                break

        # Verdict-flip guard runs on the COMPLETE reply, same as the
        # synchronous endpoint. Any appended disclaimer is streamed as a
        # trailing warning frame so the client can render it distinctly.
        full_text = "".join(parts).strip()
        if not full_text:
            yield _sse("delta", {"text": "(no reply — model returned an empty response)"})
        else:
            warned = _attach_verdict_flip_warning(full_text, stored_verdict)
            if warned != full_text and warned.startswith(full_text):
                yield _sse("warning", {"text": warned[len(full_text):]})
        yield _sse("done", {})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
