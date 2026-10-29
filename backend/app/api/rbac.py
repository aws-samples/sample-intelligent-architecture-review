# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Role-based access control (RBAC).

Four roles with separation of duties:

  - admin     — everything, including managing user roles.
  - validator — upload SDDs + run validation, view results, view library,
                chat KB. CANNOT WRITE to the standards library.
  - curator   — upload standards docs, edit the library, approve/reject
                proposed rules, chat with the AI rule curator. CANNOT run
                validation.
  - viewer    — read-only + chat KB.

Two similarly-named endpoints belong to DIFFERENT roles, which is deliberate:
``POST /standards/curate`` picks the most relevant existing rules for one
document (read-only rule selection, used by the Run Validation dialog) and
allows validator OR curator; ``POST /standards/chat`` is the AI rule curator
that proposes library changes, and is curator-only.

Enforcement layers on top of the existing auth choke point: every router
already carries ``Depends(get_current_user)`` (main.py). ``require_role(...)``
is an ADDITIONAL dependency added to write endpoints; it resolves the caller's
role and 403s if it isn't in the allowed set.

Role source is hybrid (config-flippable to Entra later without code change):
  1. a ``cognito:groups`` claim on the token (present once Entra→Cognito group
     mapping is wired), else
  2. a per-user assignment in the ``user-roles`` DDB table, else
  3. ``BOOTSTRAP_ADMIN_EMAIL`` (seed the first admin), else
  4. ``viewer`` (safe default — an unknown user can read but not change).
"""

from __future__ import annotations

import logging
import os
from enum import Enum

from fastapi import Depends, HTTPException

from app.api.auth import get_current_user

logger = logging.getLogger(__name__)


class Role(str, Enum):
    ADMIN = "admin"
    VALIDATOR = "validator"
    CURATOR = "curator"
    VIEWER = "viewer"


_VALID_ROLES = {r.value for r in Role}


def _bootstrap_admin_emails() -> set[str]:
    """Comma-separated emails that are always admin — used to seed the first
    admin before any role rows exist. Lower-cased for case-insensitive match."""
    raw = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _role_from_groups(claims: dict) -> str | None:
    """Map a Cognito/Entra ``cognito:groups`` claim to a role, if present.

    The claim is a list (Cognito) or comma string. We pick the HIGHEST-
    privilege role named among the groups so a user in both 'curator' and
    'admin' groups resolves to admin. Absent claim → None (fall through).

    ONLY trusted on a federated token. The claim is derived by the pre-token
    Lambda from the ``custom:saml_groups`` attribute, which is meaningful only
    when an identity provider asserted it. On a native (username/password)
    login that attribute is not IdP-controlled, so honouring the claim there
    would let a user who can write their own attributes pick their own role.
    Native logins fall through to the role table instead."""
    groups = claims.get("cognito:groups")
    if not groups:
        return None
    if not claims.get("identities"):
        # No federated identity on this token → the groups claim is not
        # IdP-asserted and must not decide the role.
        return None
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",")]
    named = {str(g).strip().lower() for g in groups} & _VALID_ROLES
    if not named:
        return None
    for r in (Role.ADMIN, Role.CURATOR, Role.VALIDATOR, Role.VIEWER):
        if r.value in named:
            return r.value
    return None


def resolve_role(claims: dict) -> str:
    """Resolve the caller's role from token claims. Never raises — defaults to
    viewer so a resolution failure fails CLOSED (least privilege), not open."""
    # 1. cognito:groups claim (Entra-mapped groups, when wired).
    from_groups = _role_from_groups(claims)
    if from_groups:
        return from_groups

    sub = claims.get("sub") or ""
    email = (claims.get("email") or "").strip().lower()

    # 2. explicit per-user assignment in the DDB table.
    try:
        from app.services.user_roles_store import user_roles_store
        assigned = user_roles_store.get_role(sub)
        if assigned in _VALID_ROLES:
            return assigned
    except Exception:  # noqa: BLE001 — store failure must not open access
        logger.warning("resolve_role: user_roles_store lookup failed", exc_info=True)

    # 3. bootstrap admin (seed the first admin before any rows exist).
    #    Requires a VERIFIED email: the address is self-assertable otherwise,
    #    and matching it grants admin. Cognito omits email_verified on some
    #    token shapes, so treat an absent claim as unverified rather than
    #    assuming good faith — an admin-created user gets a role-table row
    #    (branch 2) anyway, which is the intended path once bootstrapped.
    if email and email in _bootstrap_admin_emails():
        if claims.get("email_verified") in (True, "true", "True"):
            return Role.ADMIN.value
        logger.warning(
            "bootstrap-admin match for %s ignored: email is not verified", email
        )

    # 4. safe default.
    return Role.VIEWER.value


def require_role(*allowed: Role):
    """FastAPI dependency factory: allow the request only if the caller's role
    is in ``allowed``. Admin is ALWAYS allowed (it implies every capability),
    so callers list only the non-admin roles that also qualify.

    Usage:  dependencies=[Depends(require_role(Role.CURATOR))]
    (admin is implicitly permitted; validator/viewer get 403 here).
    """
    allowed_values = {r.value for r in allowed} | {Role.ADMIN.value}

    async def _dep(claims: dict = Depends(get_current_user)) -> dict:
        role = resolve_role(claims)
        if role not in allowed_values:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Requires role: {', '.join(sorted(allowed_values))}. "
                    f"Your role: {role}."
                ),
            )
        # Stash the resolved role on the claims dict so handlers can read it
        # (e.g. the documents upload split by document_type).
        claims["_role"] = role
        return claims

    return _dep


def actor_email(claims: dict) -> str:
    """The caller's email, normalised. Ownership is recorded by email because
    that is what ``uploaded_by`` / ``created_by`` already store."""
    return str((claims or {}).get("email", "") or "").strip().lower()


def is_admin(claims: dict) -> bool:
    """True if the caller resolves to admin. Reads the role stashed by
    ``require_role`` when present, else resolves it."""
    role = (claims or {}).get("_role") or resolve_role(claims or {})
    return role == Role.ADMIN.value


def require_owner_or_admin(claims: dict, owner: str | None, *, what: str) -> None:
    """Authorize a mutation of someone else's resource. Raises 403 otherwise.

    Role gates answer "may this user ever do X"; they cannot answer "may this
    user do X to THIS row". Without this, any validator could retier or cancel
    another user's work — and the declared tier decides which rules a document
    is judged against, so that silently changes someone else's verdict.

    Unowned rows (``owner`` empty — written before ownership was recorded) stay
    mutable by any caller holding the role. Refusing them instead would strand
    existing documents with nobody able to fix them, and they predate the
    control; admin can always act.
    """
    if not owner:
        return
    if is_admin(claims):
        return
    if actor_email(claims) == str(owner).strip().lower():
        return
    raise HTTPException(
        status_code=403,
        detail=f"Only the owner of this {what} or an admin can change it.",
    )
