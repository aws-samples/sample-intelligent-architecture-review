# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the Cognito pre-token-generation Lambda.

The handler lives outside the `app` package (it's packaged as a standalone
Lambda zip under backend/lambdas/pretoken/), so we load it by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HANDLER_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "lambdas" / "pretoken" / "handler.py"
)
_spec = importlib.util.spec_from_file_location("pretoken_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _event(saml_groups: str | None):
    """Minimal v1 pre-token event with the given custom:saml_groups value."""
    attrs = {}
    if saml_groups is not None:
        attrs["custom:saml_groups"] = saml_groups
    return {
        "request": {"userAttributes": attrs},
        "response": {},
    }


def _override(result: dict):
    """Extract groupsToOverride, or None if the handler set no override.

    Walks with an explicit ``or {}`` at each hop rather than ``get(k, {})``:
    Cognito sends these keys present-and-null, and a default only applies when
    the key is ABSENT, so the dict-default form would return None and then raise
    AttributeError on the next .get().
    """
    node = result or {}
    for key in ("response", "claimsOverrideDetails", "groupOverrideDetails"):
        node = (node or {}).get(key) or {}
    return node.get("groupsToOverride")


@pytest.mark.parametrize("role", ["admin", "curator", "validator", "viewer"])
def test_single_group_maps_to_role(role):
    result = handler.lambda_handler(_event(role))
    assert _override(result) == [role]


def test_multiple_groups_all_valid_roles_emitted():
    result = handler.lambda_handler(_event("viewer,curator,admin"))
    # Stable, privilege-descending order regardless of input order.
    assert _override(result) == ["admin", "curator", "viewer"]


def test_semicolon_delimiter_and_whitespace():
    result = handler.lambda_handler(_event(" curator ; viewer "))
    assert _override(result) == ["curator", "viewer"]


def test_case_insensitive():
    result = handler.lambda_handler(_event("ADMIN,Curator"))
    assert _override(result) == ["admin", "curator"]


def test_unknown_groups_dropped():
    result = handler.lambda_handler(_event("Domain Admins,SomeEntraGroup"))
    # No valid role -> no override at all (falls through to DDB/viewer).
    assert _override(result) is None


def test_partial_unknown_keeps_valid_only():
    result = handler.lambda_handler(_event("curator,Domain Admins"))
    assert _override(result) == ["curator"]


def test_duplicate_groups_deduped():
    result = handler.lambda_handler(_event("curator,curator,curator"))
    assert _override(result) == ["curator"]


def test_empty_attribute_no_override():
    result = handler.lambda_handler(_event(""))
    assert _override(result) is None


def test_missing_attribute_no_override():
    result = handler.lambda_handler(_event(None))
    assert _override(result) is None


def test_missing_request_key_does_not_raise():
    # Defensive: a malformed event must not crash token generation.
    result = handler.lambda_handler({})
    assert _override(result) is None


def test_event_is_returned():
    ev = _event("admin")
    result = handler.lambda_handler(ev)
    assert result is ev  # mutates and returns the same object


# --- the real Cognito event shape -------------------------------------------
#
# The events above build "response": {} — the keys ABSENT. Cognito really sends
# them PRESENT AND NULL. That difference hid a production crash: the handler used
# response.setdefault("claimsOverrideDetails", {}), which only inserts when a key
# is absent, so it returned the existing None and assigning into it raised
# TypeError: 'NoneType' object does not support item assignment. Token generation
# failed and sign-in hung at the code-for-token exchange.
#
# It stayed hidden because the crash needs a NON-EMPTY role list to reach that
# branch, and until the app client was allowed to write custom:saml_groups the
# attribute was always empty. Fixing the attribute made this path reachable.


def _real_event(saml_groups: str, *, override_null: bool = True) -> dict:
    """A pre-token event shaped the way Cognito actually sends it."""
    return {
        "request": {
            "userAttributes": {
                "custom:saml_groups": saml_groups,
                "email": "user@example.com",
                "sub": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
            "groupConfiguration": {
                "groupsToOverride": [],
                "iamRolesToOverride": [],
                "preferredRole": None,
            },
        },
        "response": {
            "claimsOverrideDetails": None if override_null else {},
        },
    }


def test_null_claims_override_details_does_not_crash():
    """The exact production failure: claimsOverrideDetails present as null."""
    result = handler.lambda_handler(_real_event("admin"))
    assert _override(result) == ["admin"]


def test_null_response_does_not_crash():
    """Belt and braces — the whole response object arriving as null."""
    event = _real_event("curator")
    event["response"] = None
    result = handler.lambda_handler(event)
    assert _override(result) == ["curator"]


def test_existing_claims_override_details_is_preserved():
    """When Cognito (or another trigger) already populated the object, adding the
    group override must not discard what is there."""
    event = _real_event("admin", override_null=False)
    event["response"]["claimsOverrideDetails"] = {
        "claimsToAddOrOverride": {"tenant": "acme"},
    }
    result = handler.lambda_handler(event)
    details = result["response"]["claimsOverrideDetails"]
    assert details["groupOverrideDetails"] == {"groupsToOverride": ["admin"]}
    assert details["claimsToAddOrOverride"] == {"tenant": "acme"}, (
        "pre-existing claim overrides were dropped"
    )


def test_real_event_with_unknown_group_emits_no_override():
    """Fail closed on the production shape too: an unrecognised group yields no
    override, so resolve_role falls through instead of granting a role."""
    result = handler.lambda_handler(_real_event("Domain Admins"))
    assert _override(result) is None
