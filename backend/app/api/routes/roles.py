# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""User role management (RBAC).

Admin-only assignment of roles, plus a self-serve ``/me`` any authenticated
user can read so the frontend knows what to show. Role RESOLUTION lives in
app.api.rbac; this router just reads/writes the user_roles store.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import get_current_user
from app.api.rbac import Role, require_role, resolve_role
from app.services.user_roles_store import user_roles_store

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID = {r.value for r in Role}


class SetRoleRequest(BaseModel):
    sub: str = Field(..., description="Cognito sub of the target user")
    role: str = Field(..., description="admin | validator | curator | viewer")
    email: str = Field(default="", description="Email for human-readable listing")


@router.get("/me")
async def my_role(claims: dict = Depends(get_current_user)):
    """The caller's own resolved role + identity. Any authenticated user."""
    return {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "role": resolve_role(claims),
    }


@router.get("", dependencies=[Depends(require_role())])
async def list_roles():
    """List all explicit role assignments. Admin only (require_role() with no
    args allows admin only)."""
    return {"users": user_roles_store.list_all()}


@router.put("", dependencies=[Depends(require_role())])
async def set_role(request: SetRoleRequest):
    """Assign/reassign a user's role. Admin only."""
    role = request.role.strip().lower()
    if role not in _VALID:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{request.role}'. Valid: {', '.join(sorted(_VALID))}.",
        )
    if not request.sub.strip():
        raise HTTPException(status_code=400, detail="sub is required")
    row = user_roles_store.set_role(request.sub.strip(), role, request.email.strip())
    logger.info(f"role set: {request.sub} -> {role}")
    return row


@router.delete("/{sub}", dependencies=[Depends(require_role())])
async def remove_role(sub: str):
    """Remove a user's explicit role assignment (they revert to the default
    resolution — viewer, unless a group claim / bootstrap email applies).
    Admin only."""
    removed = user_roles_store.remove_role(sub)
    if not removed:
        raise HTTPException(status_code=404, detail=f"No role assignment for {sub}")
    return {"message": f"Role assignment for {sub} removed"}
