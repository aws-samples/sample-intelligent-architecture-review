# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom on-prem icon legend API.

CRUD + two authoring modes:
  - TEXT: create/polish a written definition.
  - IMAGE: upload → a multimodal model DERIVES the definition (draft) → the user
    reviews/approves it. The model-derived caption + the image both feed the
    validator, so a mislabeled upload can't assert a false mapping.
Plus bulk upload (many images → async interpret job → review queue; or a
CSV/JSON of text descriptions → drafts). Writes require curator+; reads open.
Mutations record audit events.
"""

from __future__ import annotations

import csv
import io
import json as _json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response,
    UploadFile,
)
from pydantic import BaseModel, Field

from app.api.auth import get_current_user
from app.api.rbac import Role, require_role, resolve_role
from app.services.audit_store import audit_store
from app.services.icon_legend_store import icon_legend_store, VALID_ICON_TYPES

logger = logging.getLogger(__name__)
router = APIRouter()

# Bounds for bulk uploads — keep interpret cost + request size sane.
_MAX_BULK_IMAGES = 50
_MAX_BULK_TEXT_ROWS = 500
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_BULK_TEXT_BYTES = 5 * 1024 * 1024
# Guard against decompression bombs: a small compressed image can decode to a
# huge bitmap (fitz.Pixmap allocates the full canvas). Reject anything whose
# decoded pixel count exceeds this before we materialise it.
_MAX_IMAGE_PIXELS = 25_000_000  # ~5000×5000
# Cap concurrent multimodal interpret calls in a bulk job so 50 images don't
# fan out into 50 simultaneous Bedrock calls (throttling + memory). Small,
# since each call is a heavy Opus round-trip.
_BULK_IMAGE_CONCURRENCY = 3


def _curator_or_admin(claims: dict) -> bool:
    return resolve_role(claims) in (Role.CURATOR.value, Role.ADMIN.value)

# Bulk-image-interpret job tracker. The UI polls progress, but the service
# autoscales to several ECS tasks, so a poll request may hit a task that never
# registered the job — an in-process dict alone would 404 spuriously. So job
# state is WRITTEN THROUGH to DDB (icon_legend_store.put_bulk_job) and any task
# can read it; the local dict is just a fast same-task cache. The lock
# serializes counter updates within a process.
_bulk_jobs: dict[str, dict] = {}
_bulk_lock = threading.Lock()


def _bump_job(job_id: str, field: str) -> None:
    """Increment a job counter and persist the snapshot (under the lock so the
    persisted value matches the in-memory counter — no torn write)."""
    with _bulk_lock:
        job = _bulk_jobs.get(job_id)
        if job is None:
            return
        job[field] = job.get(field, 0) + 1
        icon_legend_store.put_bulk_job(dict(job))


def _finish_job(job_id: str) -> None:
    with _bulk_lock:
        job = _bulk_jobs.get(job_id)
        if job is None:
            return
        job["status"] = "completed"
        icon_legend_store.put_bulk_job(dict(job))


def _audit(claims, action, entity_id, detail=None):
    try:
        audit_store.record_from_claims(
            claims, action=action, entity_type="icon_legend",
            entity_id=entity_id, detail=detail,
        )
    except Exception:  # noqa: BLE001
        logger.exception("audit record failed (non-fatal)")


def _guard_image_dimensions(raw: bytes) -> None:
    """Decompression-bomb guard. The 2MB byte cap bounds INPUT size, not decoded
    pixels — a small compressed image can decode to a huge bitmap. We must reject
    an oversized canvas BEFORE any full decode allocates it. PIL reads only the
    header for `.size` (lazy — it does not decode pixel data), so this is cheap
    and, critically, runs before fitz.Pixmap() eagerly materialises the bitmap."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size  # from the header; no pixel decode
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 — unreadable header; let fitz report the real error
        return
    if w * h > _MAX_IMAGE_PIXELS:
        raise ValueError(
            f"Image too large ({w}×{h}); max {_MAX_IMAGE_PIXELS} pixels"
        )


def _normalize_png(raw: bytes) -> bytes:
    """Convert arbitrary image bytes to PNG (so the diagram-prompt image block's
    declared 'png' format always matches). Raises ValueError on unsupported."""
    # Reject decompression bombs from the header BEFORE fitz.Pixmap(raw), which
    # eagerly decodes the full bitmap at construction (so a post-construction
    # size check would already have paid the memory cost).
    _guard_image_dimensions(raw)
    try:
        import fitz  # PyMuPDF — already a dependency
        pix = fitz.Pixmap(raw)
        # Belt-and-braces: also cap once decoded (covers formats PIL couldn't
        # read a size for but fitz can).
        if pix.width * pix.height > _MAX_IMAGE_PIXELS:
            raise ValueError(
                f"Image too large ({pix.width}×{pix.height}); "
                f"max {_MAX_IMAGE_PIXELS} pixels"
            )
        if pix.alpha:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        return pix.tobytes("png")
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Unsupported image: {e}")


def _store_icon_image(icon_id: str, png_bytes: bytes) -> str:
    from app.services.document_store import document_store
    s3_key = f"icon-legend/{icon_id}/{uuid.uuid4().hex[:8]}.png"
    document_store.upload_document(png_bytes, s3_key, content_type="image/png")
    return s3_key


def _delete_icon_image(s3_key: Optional[str]) -> None:
    """Best-effort delete of an icon's S3 object. Never raises — an orphaned
    object is preferable to a failed delete blocking the row removal."""
    if not s3_key:
        return
    try:
        from app.services.document_store import document_store
        document_store.delete_document(s3_key)
    except Exception:  # noqa: BLE001
        logger.warning(f"icon image S3 delete failed (non-fatal): {s3_key}")


class CreateIconRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    icon_type: str = Field(..., description=f"One of: {sorted(VALID_ICON_TYPES)}")
    description: str = Field(default="", max_length=600)
    hosting_scope: str = Field(default="on_prem")


class UpdateIconRequest(BaseModel):
    label: Optional[str] = Field(default=None, max_length=120)
    icon_type: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=600)
    hosting_scope: Optional[str] = None
    # No `status` field: activation (draft→active) must flow through /approve so
    # it's audited as an approval and stamps approver provenance. PUT edits
    # content only; it never changes the approval state.


@router.get("")
async def list_icons(
    status: Optional[str] = "active",
    batch: Optional[str] = None,
    claims: dict = Depends(get_current_user),
):
    """List legend entries. status defaults to active (any authenticated
    caller). Drafts are unreviewed, possibly-wrong AI content, so the review
    queue (status != active) is curator/admin only — viewers must not see
    unapproved interpretations."""
    if (status or "active") != "active" and not _curator_or_admin(claims):
        raise HTTPException(
            status_code=403, detail="Only curators can view the review queue"
        )
    return {"icons": icon_legend_store.list_all(status=status, bulk_batch_id=batch)}


@router.post("", status_code=201)
async def create_icon(
    request: CreateIconRequest,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Create a TEXT-authored legend entry (goes straight to active)."""
    if request.icon_type.strip().lower() not in VALID_ICON_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"icon_type must be one of {sorted(VALID_ICON_TYPES)}",
        )
    item = icon_legend_store.create(
        label=request.label,
        icon_type=request.icon_type,
        description=request.description,
        hosting_scope=request.hosting_scope,
        created_by=str((claims or {}).get("email", "") or ""),
        source_kind="text",
        status="active",
    )
    _audit(claims, "icon_legend.create", item["id"], detail={"label": item["label"]})
    return item


class PolishRequest(BaseModel):
    label: str = Field(default="", max_length=120)
    icon_type: str = Field(default="other")
    description: str = Field(..., min_length=1, max_length=600)


@router.post("/polish")
async def polish_icon_description(
    request: PolishRequest,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Return an AI-polished version of a text description (no persistence — the
    UI shows before/after and the user decides). Falls back to the original."""
    from app.services.icon_interpret import polish_description

    polished = polish_description(request.label, request.icon_type, request.description)
    return {"original": request.description, "polished": polished}


@router.post("/interpret-image", status_code=201)
async def interpret_image(
    file: UploadFile = File(...),
    hosting_scope: str = Form("on_prem"),
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """IMAGE mode: upload one icon → store it → a multimodal model derives the
    definition → persist as a DRAFT (status='draft') for the user to review and
    approve. NOT injected into validation until approved."""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Icon image too large (max 2MB)")
    try:
        png = _normalize_png(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    from app.services.icon_interpret import interpret_icon
    derived = interpret_icon(png)

    # Create the draft first (need its id for the S3 key), then attach image.
    draft = icon_legend_store.create(
        label=derived["label"] or file.filename or "custom icon",
        icon_type=derived["icon_type"],
        description=derived["description"],
        hosting_scope=hosting_scope,
        created_by=str((claims or {}).get("email", "") or ""),
        source_kind="image",
        status="draft",
        ai_generated=True,
        ai_model=derived.get("ai_model", ""),
        confidence=derived.get("confidence", ""),
    )
    try:
        s3_key = _store_icon_image(draft["id"], png)
        draft = icon_legend_store.update(draft["id"], {"image_s3_key": s3_key})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"interpret-image: S3 store failed for {draft['id']}: {e}")
    _audit(claims, "icon_legend.interpret_image", draft["id"],
           detail={"confidence": derived.get("confidence")})
    return draft


class ApproveIconRequest(BaseModel):
    """Optional edits to apply when approving a draft (all optional)."""
    label: Optional[str] = Field(default=None, max_length=120)
    icon_type: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=600)
    hosting_scope: Optional[str] = None


@router.post("/{icon_id}/approve")
async def approve_icon(
    icon_id: str,
    request: Optional[ApproveIconRequest] = None,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Approve a draft (image- or bulk-derived) → active. Optionally apply the
    user's edits in the same call. Only then does it reach the validator."""
    existing = icon_legend_store.get(icon_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Icon {icon_id} not found")
    fields: dict = {}
    if request is not None:
        fields = request.model_dump(exclude_none=True)
    fields["status"] = "active"
    # Stamp approver identity + time ON the row (not just the audit log) so an
    # official entry self-describes who made a model-derived definition
    # authoritative — the core accountability requirement of the review gate.
    fields["approved_by"] = str((claims or {}).get("email", "") or "")
    fields["approved_at"] = datetime.now(timezone.utc).isoformat()
    updated = icon_legend_store.update(icon_id, fields)
    _audit(claims, "icon_legend.approve", icon_id,
           detail={"label": (updated or {}).get("label")})
    return updated


@router.put("/{icon_id}")
async def update_icon(
    icon_id: str,
    request: UpdateIconRequest,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    updated = icon_legend_store.update(icon_id, request.model_dump(exclude_none=True))
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Icon {icon_id} not found")
    _audit(claims, "icon_legend.update", icon_id)
    return updated


@router.delete("/{icon_id}", status_code=204)
async def delete_icon(
    icon_id: str,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    entry = icon_legend_store.get(icon_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Icon {icon_id} not found")
    icon_legend_store.delete(icon_id)
    # Best-effort S3 cleanup so a deleted entry doesn't leave an orphaned image
    # object (data-retention hygiene in the regulated context + storage leak).
    _delete_icon_image(entry.get("image_s3_key"))
    _audit(claims, "icon_legend.delete", icon_id)
    return None


@router.get("/{icon_id}/image")
async def get_icon_image(
    icon_id: str,
    claims: dict = Depends(get_current_user),
):
    """Serve the icon's stored image bytes so the reviewer SEES the pixels the
    model interpreted before approving a draft (the whole point of the review
    gate — approving derived text blind would defeat it). Drafts hold
    unreviewed AI content, so their image is curator/admin only; active entries
    are visible to any authenticated caller."""
    entry = icon_legend_store.get(icon_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Icon {icon_id} not found")
    if (entry.get("status") or "active") != "active" and not _curator_or_admin(claims):
        raise HTTPException(status_code=403, detail="Only curators can view drafts")
    key = entry.get("image_s3_key")
    if not key:
        raise HTTPException(status_code=404, detail="Icon has no image")
    from app.services.document_store import document_store
    try:
        data = document_store.download_document(key)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to fetch image: {e}")
    if not data:
        raise HTTPException(status_code=404, detail="Image not found in storage")
    # This URL keys on the STABLE icon_id, and the underlying bytes can change
    # (replace-image / re-interpret), so don't let the browser serve a stale
    # thumbnail. no-store keeps it private + always-fresh; icons are tiny.
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "private, no-store"})


@router.post("/{icon_id}/reinterpret", status_code=201)
async def reinterpret_icon(
    icon_id: str,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Re-run the multimodal interpretation on an entry's EXISTING stored image
    and re-draft it (status→draft with a fresh derived definition) for review.
    Lets a curator correct a wrong approved definition without re-uploading."""
    entry = icon_legend_store.get(icon_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Icon {icon_id} not found")
    key = entry.get("image_s3_key")
    if not key:
        raise HTTPException(status_code=400, detail="Icon has no image to re-interpret")
    from app.services.document_store import document_store
    try:
        raw = document_store.download_document(key)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to fetch image: {e}")
    if not raw:
        raise HTTPException(status_code=404, detail="Image not found in storage")

    from app.services.icon_interpret import interpret_icon
    derived = interpret_icon(raw)
    # interpret_icon degrades to an empty ai_model + low-confidence stub when the
    # model is unavailable. In that case DON'T overwrite the entry's existing
    # (possibly approved) definition with the stub — just send it back to review
    # unchanged so the curator can retry, rather than destroying good data.
    degraded = not derived.get("ai_model")
    if degraded:
        fields = {"status": "draft"}
        detail_conf = "unavailable"
    else:
        fields = {
            "label": derived["label"] or entry.get("label"),
            "icon_type": derived["icon_type"],
            "description": derived["description"],
            "status": "draft",
            "source_kind": "image",
            "ai_generated": True,
            "ai_model": derived.get("ai_model", ""),
            "confidence": derived.get("confidence", ""),
        }
        detail_conf = derived.get("confidence")
    # Re-drafting invalidates the prior approval — clear stale provenance so an
    # unreviewed re-interpretation never appears already-approved.
    fields["approved_by"] = ""
    fields["approved_at"] = ""
    updated = icon_legend_store.update(icon_id, fields)
    if degraded:
        # Signal the transient failure without failing the request (the entry is
        # safely a draft now); the UI shows the unchanged draft for retry.
        logger.warning(f"reinterpret degraded (model unavailable) for {icon_id}")
    _audit(claims, "icon_legend.reinterpret", icon_id,
           detail={"confidence": detail_conf, "degraded": degraded})
    return updated


def _attach_image_to_entry(entry: dict, raw: bytes, claims: dict) -> dict:
    """Attach an image to an existing entry and cross-check it against the
    entry's CURRENT (human-written) definition.

    Shared by the single-row upload and the bulk filename pairing so the two
    cannot diverge on the consistency gate — a pairing that skipped the check
    would reintroduce exactly the unverified mapping the check exists to catch.

    Returns the updated row with `image_check` attached. Raises HTTPException
    for a caller-fixable problem (bad or oversized image, storage failure).
    """
    icon_id = entry["id"]
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Icon image too large (max 2MB)")
    try:
        png_bytes = _normalize_png(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    from app.services.icon_interpret import cross_check_image

    check = cross_check_image(
        png_bytes,
        label=entry.get("label", ""),
        icon_type=entry.get("icon_type", ""),
        description=entry.get("description", ""),
    )
    mismatch = check["checked"] and not check["consistent"]

    try:
        s3_key = _store_icon_image(icon_id, png_bytes)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to store icon image: {e}")

    # Replacing an image orphans the previous object; drop it after the new one
    # is safely stored so a storage failure can't lose both.
    previous_key = entry.get("image_s3_key")
    if previous_key and previous_key != s3_key:
        _delete_icon_image(previous_key)

    fields: dict = {
        "image_s3_key": s3_key,
        # Persisted so the warning survives a reload and a later reviewer sees
        # that this pairing was questioned, not just the curator who attached it.
        "image_check": {
            "consistent": check["consistent"],
            "checked": check["checked"],
            "confidence": check["confidence"],
            "observed": check["observed"],
            "reason": check["reason"],
            "model": check["ai_model"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    # A mismatch on an ACTIVE entry is the dangerous case: the crop is already
    # eligible to ship to diagram prompts as a visual example. Send it back to
    # draft so the review gate re-catches it, and clear the stale approval that
    # was granted for a different (image-less) definition.
    demoted = mismatch and entry.get("status") == "active"
    if demoted:
        fields["status"] = "draft"
        fields["approved_by"] = ""
        fields["approved_at"] = ""

    updated = icon_legend_store.update(icon_id, fields)
    _audit(claims, "icon_legend.upload_image", icon_id, detail={
        "s3_key": s3_key,
        "consistent": check["consistent"],
        "checked": check["checked"],
        "demoted_to_draft": demoted,
    })
    out = dict(updated or {})
    out["image_check_demoted"] = demoted
    return out


@router.post("/{icon_id}/image")
async def upload_icon_image(
    icon_id: str,
    file: UploadFile = File(...),
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Attach a reference icon crop (PNG/JPeg) used as a visual few-shot
    example in diagram prompts. Stored in S3; the key is saved on the entry.

    The image is cross-checked against the entry's existing human-written
    definition, because this is the one authoring path where the definition did
    not come from the pixels: the curator wrote the text and chose the file, so
    nothing else verifies they describe the same glyph. The result is reported on
    the row rather than blocking the attach — a curator may legitimately know
    better than the model — except that a mismatch on an already-active entry
    returns it to draft.
    """
    entry = icon_legend_store.get(icon_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Icon {icon_id} not found")
    raw = await file.read()
    return _attach_image_to_entry(entry, raw, claims)


# ---------------------------------------------------------------------------
# Bulk — text (CSV/JSON) and images (async interpret job)
# ---------------------------------------------------------------------------


def _rows_from_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader]


def _rows_from_json(text: str) -> list[dict]:
    data = _json.loads(text)
    if isinstance(data, dict):
        data = data.get("icons") or data.get("rows") or []
    if not isinstance(data, list):
        raise ValueError("JSON must be a list of objects (or {icons:[...]})")
    return [r for r in data if isinstance(r, dict)]


@router.post("/bulk/text", status_code=201)
async def bulk_text(
    file: UploadFile = File(...),
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Bulk-create TEXT drafts from a CSV or JSON file. Columns/keys: label
    (required), icon_type (required, from the enum), description, hosting_scope.
    Rows land as DRAFTS in a review batch for approval. Strict parse — bad rows
    are reported, not silently dropped."""
    raw_bytes = await file.read()
    # Cap payload size BEFORE parse: the row cap is only checked post-parse, so
    # without this a multi-hundred-MB file would be fully read + parsed into
    # memory first (DoS). Generous vs 500 rows of short text.
    if len(raw_bytes) > _MAX_BULK_TEXT_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")
    raw = raw_bytes.decode("utf-8", errors="replace")
    name = (file.filename or "").lower()
    try:
        rows = _rows_from_json(raw) if name.endswith(".json") else _rows_from_csv(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not parse file: {e}")
    if not rows:
        raise HTTPException(status_code=400, detail="No rows found in file")
    if len(rows) > _MAX_BULK_TEXT_ROWS:
        raise HTTPException(status_code=400, detail=f"Too many rows (max {_MAX_BULK_TEXT_ROWS})")

    batch_id = f"iconbatch-{uuid.uuid4().hex[:8]}"
    created, errors = [], []
    for i, r in enumerate(rows):
        label = str(r.get("label", "") or "").strip()
        itype = str(r.get("icon_type", "") or "").strip().lower()
        if not label:
            errors.append({"row": i + 1, "error": "missing label"})
            continue
        if itype not in VALID_ICON_TYPES:
            errors.append({"row": i + 1, "error": f"invalid icon_type '{itype}'"})
            continue
        item = icon_legend_store.create(
            label=label[:120],
            icon_type=itype,
            description=str(r.get("description", "") or "")[:600],
            hosting_scope=str(r.get("hosting_scope", "on_prem") or "on_prem"),
            created_by=str((claims or {}).get("email", "") or ""),
            source_kind="text",
            status="draft",
            bulk_batch_id=batch_id,
            image_filename=_norm_filename(r.get("image_filename")),
        )
        created.append(item["id"])
    _audit(claims, "icon_legend.bulk_text", batch_id,
           detail={"created": len(created), "errors": len(errors)})
    return {"batch_id": batch_id, "created": len(created), "errors": errors}


def _norm_filename(value: object) -> str:
    """Normalize a declared image filename for pairing.

    Basename only, lower-cased: a spreadsheet may carry a path from the author's
    machine ("C:\\icons\\fw.png") while the upload sends only "fw.png", and case
    differs freely between a CSV and a file picker. Comparing raw strings would
    silently fail to match rows the curator expected to pair.
    """
    import os

    name = str(value or "").strip().replace("\\", "/")
    return os.path.basename(name).strip().lower()[:255]


@router.post("/bulk/{batch_id}/pair-images")
async def pair_batch_images(
    batch_id: str,
    files: list[UploadFile] = File(...),
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Pair uploaded image files to a text batch's drafts by filename.

    Rows declare `image_filename` in the CSV/JSON; each uploaded file is matched
    to the row that named it. Matching is on the declared filename rather than
    the label because a curator edits labels during review, which would silently
    break the pairing.

    Every pair runs the same consistency cross-check as a single attach, so the
    bulk path cannot be used to bypass it. Unmatched files and unmatched rows are
    both reported — a silent partial pairing would read as complete, leaving rows
    without the visual example the curator believed they had attached.
    """
    drafts = icon_legend_store.list_all(status="draft", bulk_batch_id=batch_id)
    if not drafts:
        raise HTTPException(status_code=404, detail=f"No drafts for batch {batch_id}")
    if len(files) > _MAX_BULK_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files (max {_MAX_BULK_IMAGES})",
        )

    by_name: dict[str, dict] = {}
    for d in drafts:
        declared = _norm_filename(d.get("image_filename"))
        if declared:
            by_name.setdefault(declared, d)

    paired, unmatched_files, failures, mismatches = [], [], [], []
    used: set[str] = set()
    for f in files:
        fname = _norm_filename(f.filename)
        row = by_name.get(fname)
        if row is None or row["id"] in used:
            unmatched_files.append(f.filename or "")
            continue
        try:
            raw = await f.read()
            updated = _attach_image_to_entry(row, raw, claims)
        except HTTPException as e:
            # One bad file must not abort the batch — report it and continue, so
            # a single oversized crop doesn't strand every other pairing.
            failures.append({"file": f.filename or "", "error": str(e.detail)})
            continue
        used.add(row["id"])
        check = (updated or {}).get("image_check") or {}
        entry = {"file": f.filename or "", "icon_id": row["id"], "label": row.get("label", "")}
        paired.append(entry)
        if check.get("checked") and not check.get("consistent"):
            mismatches.append({**entry, "reason": check.get("reason", ""),
                               "observed": check.get("observed", "")})

    # Rows the curator asked to pair but no file arrived for. A row that declared
    # no filename at all is not "unmatched" — it never expected an image.
    unmatched_rows = [
        {"icon_id": d["id"], "label": d.get("label", ""),
         "expected_file": d.get("image_filename", "")}
        for d in drafts
        if _norm_filename(d.get("image_filename")) and d["id"] not in used
    ]

    _audit(claims, "icon_legend.bulk_pair_images", batch_id, detail={
        "paired": len(paired),
        "unmatched_files": len(unmatched_files),
        "unmatched_rows": len(unmatched_rows),
        "mismatches": len(mismatches),
        "failures": len(failures),
    })
    return {
        "batch_id": batch_id,
        "paired": paired,
        "mismatches": mismatches,
        "unmatched_files": unmatched_files,
        "unmatched_rows": unmatched_rows,
        "failures": failures,
    }


def _interpret_one_bulk_image(job_id: str, batch_id: str, fname: str, raw: bytes,
                              hosting_scope: str, actor_email: str) -> None:
    """Interpret a single bulk image → draft + attach image. Updates job
    counters under the lock. Swallows its own errors (bumps failed)."""
    from app.services.icon_interpret import interpret_icon
    try:
        png = _normalize_png(raw)
        derived = interpret_icon(png)
        draft = icon_legend_store.create(
            label=derived["label"] or fname or "custom icon",
            icon_type=derived["icon_type"],
            description=derived["description"],
            hosting_scope=hosting_scope,
            created_by=actor_email,
            source_kind="image",
            status="draft",
            ai_generated=True,
            ai_model=derived.get("ai_model", ""),
            confidence=derived.get("confidence", ""),
            bulk_batch_id=batch_id,
        )
        try:
            key = _store_icon_image(draft["id"], png)
            icon_legend_store.update(draft["id"], {"image_s3_key": key})
        except Exception:  # noqa: BLE001
            pass
        _bump_job(job_id, "done")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"bulk image interpret failed for {fname}: {e}")
        _bump_job(job_id, "failed")


def _run_bulk_image_job(job_id: str, batch_id: str, images: list[tuple[str, bytes]],
                        hosting_scope: str, actor_email: str) -> None:
    """Background: interpret images → drafts, with bounded concurrency so a big
    batch doesn't fan 50 heavy Bedrock calls out at once. Updates job progress;
    marks completed even if every image failed."""
    try:
        with ThreadPoolExecutor(max_workers=_BULK_IMAGE_CONCURRENCY) as pool:
            futures = [
                pool.submit(_interpret_one_bulk_image, job_id, batch_id, fname,
                            raw, hosting_scope, actor_email)
                for fname, raw in images
            ]
            for f in futures:
                f.result()  # each swallows its own errors; surface pool faults
    except Exception:  # noqa: BLE001
        logger.exception(f"bulk image job {job_id} pool error")
    _finish_job(job_id)


@router.post("/bulk/images", status_code=202)
async def bulk_images(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    hosting_scope: str = Form("on_prem"),
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Bulk-upload icon images → async interpret job → drafts in a review batch.
    Returns a job_id + batch_id; poll GET /bulk/jobs/{job_id} for progress."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) > _MAX_BULK_IMAGES:
        raise HTTPException(status_code=400, detail=f"Too many images (max {_MAX_BULK_IMAGES})")
    images: list[tuple[str, bytes]] = []
    skipped: list[dict] = []
    for f in files:
        raw = await f.read()
        if not raw:
            skipped.append({"file": f.filename or "icon", "error": "empty"})
        elif len(raw) > _MAX_IMAGE_BYTES:
            skipped.append({"file": f.filename or "icon", "error": "too large (max 2MB)"})
        else:
            images.append((f.filename or "icon", raw))
    if not images:
        raise HTTPException(status_code=400, detail="No valid images (empty or >2MB)")

    # Full uuid hex (not [:8]) so two concurrent jobs can't collide on a key.
    job_id = f"iconjob-{uuid.uuid4().hex}"
    batch_id = f"iconbatch-{uuid.uuid4().hex[:12]}"
    job = {
        "job_id": job_id, "batch_id": batch_id, "total": len(images),
        "done": 0, "failed": 0, "status": "running",
    }
    with _bulk_lock:
        _bulk_jobs[job_id] = dict(job)
    # Persist the initial state so a poll landing on another ECS task can see it.
    icon_legend_store.put_bulk_job(dict(job))
    background_tasks.add_task(
        _run_bulk_image_job, job_id, batch_id, images, hosting_scope,
        str((claims or {}).get("email", "") or ""),
    )
    _audit(claims, "icon_legend.bulk_images", batch_id,
           detail={"count": len(images), "skipped": len(skipped)})
    return {"job_id": job_id, "batch_id": batch_id, "total": len(images), "skipped": skipped}


@router.get("/bulk/jobs/{job_id}")
async def bulk_job_status(job_id: str):
    # Prefer the local in-process snapshot (fresh, same task), but fall back to
    # the persisted row so a poll routed to a different task still resolves.
    with _bulk_lock:
        job = _bulk_jobs.get(job_id)
        job = dict(job) if job else None  # copy under lock — no torn snapshot
    if job is None:
        job = icon_legend_store.get_bulk_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@router.post("/bulk/{batch_id}/approve-all")
async def approve_batch(
    batch_id: str,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Approve every draft in a review batch → active, stamping the approver."""
    drafts = icon_legend_store.list_all(status="draft", bulk_batch_id=batch_id)
    if not drafts:
        raise HTTPException(status_code=404, detail=f"No drafts for batch {batch_id}")
    approver = str((claims or {}).get("email", "") or "")
    now = datetime.now(timezone.utc).isoformat()
    approved = 0
    for d in drafts:
        icon_legend_store.update(
            d["id"],
            {"status": "active", "approved_by": approver, "approved_at": now},
        )
        approved += 1
    _audit(claims, "icon_legend.batch_approve_all", batch_id, detail={"approved": approved})
    return {"batch_id": batch_id, "approved": approved}


@router.post("/bulk/{batch_id}/reject-all")
async def reject_batch(
    batch_id: str,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Reject (delete) every DRAFT in a review batch.

    Scoped to drafts on purpose. An entry that has already been approved is
    active and may be shaping validation prompts, so a batch reject must not
    remove it as a side effect of clearing the batch it arrived in — that is a
    separate, deliberate deletion.

    Rejection is destructive rather than a status change, matching the
    single-draft reject: a rejected definition should leave no trace that could
    later be promoted, and the source images can be re-uploaded.
    """
    drafts = icon_legend_store.list_all(status="draft", bulk_batch_id=batch_id)
    if not drafts:
        raise HTTPException(status_code=404, detail=f"No drafts for batch {batch_id}")
    rejected = 0
    for d in drafts:
        icon_legend_store.delete(d["id"])
        # Best-effort S3 cleanup, same as the single delete: without it a
        # rejected batch leaves orphaned image objects behind.
        _delete_icon_image(d.get("image_s3_key"))
        rejected += 1
    _audit(claims, "icon_legend.batch_reject_all", batch_id, detail={"rejected": rejected})
    return {"batch_id": batch_id, "rejected": rejected}
