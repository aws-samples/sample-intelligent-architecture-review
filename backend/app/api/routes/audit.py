# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Audit-trail read API — "who did what, when".

Write side is the audit_store, called (best-effort) from mutation handlers.
This router is read-only. Two query modes:

  GET /api/audit?entity_type=document&entity_id=doc-123
      → history for one entity (newest first), via the entity GSI.
  GET /api/audit?actor_sub=<cognito-sub>
      → activity for one user (newest first), via the actor GSI.

Router-level auth (any authenticated caller) is applied in main.py.

Entity history stays readable by any authenticated caller: it is the review
trail for a shared document, and hiding it would defeat the purpose of having
one. The PER-ACTOR mode is different — "everything user X has ever done" is a
profile of a colleague's activity, not review evidence, so it is restricted to
the caller themselves or an admin.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import get_current_user
from app.api.rbac import is_admin
from app.services.audit_store import audit_store

router = APIRouter()


@router.get("")
async def list_audit_events(
    entity_type: Optional[str] = Query(default=None),
    entity_id: Optional[str] = Query(default=None),
    actor_sub: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    claims: dict = Depends(get_current_user),
):
    """Return audit events for an entity (entity_type+entity_id) or an actor
    (actor_sub), newest first. Exactly one mode must be specified.

    Querying another user's activity requires admin; anyone may query their own.
    """
    if entity_type and entity_id:
        return {"events": audit_store.list_for_entity(entity_type, entity_id, limit=limit)}
    if actor_sub:
        own_sub = str((claims or {}).get("sub", "") or "")
        if actor_sub != own_sub and not is_admin(claims):
            raise HTTPException(
                status_code=403,
                detail="You can only read your own activity. Admins can read anyone's.",
            )
        return {"events": audit_store.list_for_actor(actor_sub, limit=limit)}
    raise HTTPException(
        status_code=400,
        detail="Provide either entity_type+entity_id, or actor_sub.",
    )
