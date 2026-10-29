# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""References routes.

A reference is factual context the validator attaches alongside rules at
prompt-build time (a DC-peer table, a naming convention). It is not validated
against, but its text DOES reach the model on every matching rule — so writes
are curator-gated and audited, exactly like the rule library itself. Reads are
open to any authenticated caller.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.api.rbac import Role, require_role
from app.models.reference import (
    CreateReferenceRequest,
    UpdateReferenceRequest,
)
from app.services.audit_store import audit_store
from app.services.references_store import references_store

logger = logging.getLogger(__name__)
router = APIRouter()


def _audit(claims, action, entity_id, detail=None):
    try:
        audit_store.record_from_claims(
            claims, action=action, entity_type="reference",
            entity_id=entity_id, detail=detail,
        )
    except Exception:  # noqa: BLE001
        logger.exception("audit record failed (non-fatal)")


@router.get("")
async def list_references(
    status: Optional[str] = "active",
    hosting_scope: Optional[str] = None,
):
    """List references. Defaults to status=active so the validator
    and the UI both see the same set."""
    if status not in (None, "active", "proposed", "archived"):
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    if hosting_scope not in (None, "on_prem", "cloud", "both"):
        raise HTTPException(
            status_code=400, detail=f"Invalid hosting_scope: {hosting_scope}"
        )
    refs = references_store.list_all(status=status, hosting_scope=hosting_scope)
    return {"references": refs, "total": len(refs)}


@router.post("", status_code=201)
async def create_reference(
    request: CreateReferenceRequest,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Create a reference. Curator+ only — the body is injected into validation
    prompts, so an unprivileged writer could steer a compliance verdict."""
    item = references_store.create(
        name=request.name,
        body=request.body,
        applies_to_tiers=request.applies_to_tiers,
        layer=request.layer,
        hosting_scope=request.hosting_scope,
        source_document=request.source_document,
        page_reference=request.page_reference,
        source_quote=request.source_quote,
    )
    _audit(claims, "reference.create", item["id"], detail={"name": item["name"]})
    return item


@router.get("/{reference_id}")
async def get_reference(reference_id: str):
    item = references_store.get(reference_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Reference {reference_id} not found")
    return item


@router.put("/{reference_id}")
async def update_reference(
    reference_id: str,
    request: UpdateReferenceRequest,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Apply a partial update (only the fields supplied are changed)."""
    fields = request.model_dump(exclude_unset=True, exclude_none=True)
    if not fields:
        raise HTTPException(
            status_code=400,
            detail="No fields supplied; supply at least one field to update.",
        )
    updated = references_store.update(reference_id, fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Reference {reference_id} not found")
    _audit(claims, "reference.update", reference_id, detail={"fields": sorted(fields)})
    return updated


@router.delete("/{reference_id}", status_code=204)
async def delete_reference(
    reference_id: str,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    # Check existence first so a missing row is a 404 rather than a 500.
    if references_store.get(reference_id) is None:
        raise HTTPException(status_code=404, detail=f"Reference {reference_id} not found")
    if not references_store.delete(reference_id):
        raise HTTPException(
            status_code=502,
            detail=f"Failed to delete reference {reference_id}",
        )
    _audit(claims, "reference.delete", reference_id)
    return None
