# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cognito pre-token-generation trigger — SAML groups → cognito:groups.

Cognito does NOT automatically translate a SAML group assertion into the
``cognito:groups`` claim that the backend RBAC resolver reads
(``backend/app/api/rbac.py::_role_from_groups``). This Lambda closes that gap:
the SAML IdP maps its group assertion into the Cognito custom attribute
``custom:saml_groups`` (via the identity provider's attribute mapping); this
trigger reads that attribute, keeps only values that name a real application
role, and emits them as ``cognito:groups`` on the issued token via
``groupOverrideDetails.groupsToOverride``.

Design notes:
- **stdlib only** — packaged as a zip with no dependencies.
- **Fails open to no-override:** an empty/missing attribute or all-unknown
  groups means we set nothing, so ``resolve_role`` falls through to the
  user-roles DDB table / BOOTSTRAP_ADMIN_EMAIL / viewer. We never grant a role
  we can't justify from the assertion.
- **IdP-agnostic:** the only IdP-specific knobs are the source attribute name
  (configured on the Cognito identity provider, not here) and ``GROUP_MAP``.
  For Entra, map the Entra group IDs/names to roles in ``GROUP_MAP`` — no other
  change to this handler.

Event shape (v1 pre-token-generation):
  event["request"]["userAttributes"]["custom:saml_groups"] -> "curator,viewer"
  event["response"]["claimsOverrideDetails"]["groupOverrideDetails"]
       ["groupsToOverride"] -> ["curator", "viewer"]
"""

from __future__ import annotations

import re

# The four application roles. Group values are matched case-insensitively
# against these; anything else is dropped. Keep in sync with the backend
# Role enum (backend/app/api/rbac.py). For Entra, this is where you translate
# Entra group object-IDs / display names into these role names.
VALID_ROLES = ("admin", "curator", "validator", "viewer")

# Identity mapping today (SAML group name == role name). For Entra, replace
# the values on the right with the Entra group identifiers you assign, e.g.
#   {"admin": "<entra-group-guid-1>", ...} inverted — see docs/RBAC.md.
GROUP_MAP = {role: role for role in VALID_ROLES}

# Attribute the SAML IdP populates (via the Cognito identity-provider
# attribute_mapping). Custom attributes surface with the ``custom:`` prefix.
SOURCE_ATTRIBUTE = "custom:saml_groups"

# Groups may arrive comma- or semicolon-separated (IdPs differ).
_SPLIT = re.compile(r"[,;]")

# Reverse lookup: mapped-group-value (lower) -> role.
_VALUE_TO_ROLE = {str(v).strip().lower(): role for role, v in GROUP_MAP.items()}


def _roles_from_attribute(raw: str) -> list[str]:
    """Parse the raw group attribute into a deduped, ordered list of roles.

    Order follows VALID_ROLES (stable, privilege-descending) so the emitted
    claim is deterministic regardless of assertion ordering. Unknown groups
    are dropped.
    """
    if not raw:
        return []
    seen = set()
    for token in _SPLIT.split(raw):
        key = token.strip().lower()
        role = _VALUE_TO_ROLE.get(key)
        if role:
            seen.add(role)
    # Emit in a stable, privilege-descending order.
    return [r for r in VALID_ROLES if r in seen]


def lambda_handler(event: dict, context=None) -> dict:  # noqa: ARG001 - context unused
    """Pre-token trigger entry point. Mutates and returns ``event``.

    Never raises on bad input: a malformed/absent attribute simply yields no
    override, so token generation proceeds and role resolution falls through
    to the backend's other sources.
    """
    request = event.get("request") or {}
    attrs = request.get("userAttributes") or {}
    raw = attrs.get(SOURCE_ATTRIBUTE, "") or ""

    roles = _roles_from_attribute(raw)

    if roles:
        # dict.setdefault() only inserts when the key is ABSENT. Cognito sends
        # the key present and explicitly null — {"response": {
        # "claimsOverrideDetails": None}} — so setdefault returns that None and
        # assigning into it raises TypeError, which fails token generation and
        # hangs the sign-in at the code-for-token exchange. Normalise on the
        # VALUE, not the key's presence, at both levels.
        response = event.get("response")
        if not isinstance(response, dict):
            response = {}
            event["response"] = response

        override = response.get("claimsOverrideDetails")
        if not isinstance(override, dict):
            override = {}
            response["claimsOverrideDetails"] = override

        override["groupOverrideDetails"] = {"groupsToOverride": roles}

    return event
