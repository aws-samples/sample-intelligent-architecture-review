"""deploy.sh invariants that RBAC depends on.

Parsed as text — no shell execution, no AWS — so these run in CI in
milliseconds alongside the Terraform assertions.

They exist because the script and `app.api.rbac` are coupled through data
neither one declares: the script creates Cognito users, and rbac decides their
role from the resulting token's claims. Nothing failed when that coupling broke.
`admin-create-user` sets no user attributes by default, so the intended first
administrator arrived with no `email_verified` claim, `resolve_role` correctly
refused the bootstrap-admin branch, and the documented first login silently
resolved to `viewer` — read-only, and unable to reach the admin-only
`/admin/roles` to repair itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.infra

DEPLOY_SH = Path(__file__).resolve().parents[2] / "deploy.sh"


def _read() -> str:
    assert DEPLOY_SH.exists(), f"expected deploy.sh at {DEPLOY_SH}"
    return DEPLOY_SH.read_text()


def _command_invocations(src: str, command: str) -> list[str]:
    """Every `aws cognito-idp <command> ...` call, as one flattened string each.

    The calls are line-continued, so the body is everything up to the first line
    that does not end in a backslash.
    """
    out: list[str] = []
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if f"cognito-idp {command}" not in line:
            continue
        body = [line]
        j = i
        while lines[j].rstrip().endswith("\\") and j + 1 < len(lines):
            j += 1
            body.append(lines[j])
        out.append(" ".join(part.strip().rstrip("\\").strip() for part in body))
    return out


def test_every_admin_create_user_sets_email_verified():
    """The bootstrap-admin branch of resolve_role only honours a VERIFIED email,
    so a user created without this attribute can never resolve to admin."""
    calls = _command_invocations(_read(), "admin-create-user")
    assert calls, "no admin-create-user invocation found in deploy.sh"

    missing = [c for c in calls if "Name=email_verified,Value=true" not in c]
    assert not missing, (
        "admin-create-user must set email_verified, or the created login "
        "resolves to viewer via rbac.resolve_role's safe default:\n"
        + "\n".join(missing)
    )


def test_existing_users_get_email_verified_repaired():
    """A user created before the attribute was set carries no email_verified, and
    re-running is the documented way to fix an account. Without an update on the
    already-exists path, a stuck login could never be repaired by the script."""
    src = _read()
    creates = len(_command_invocations(src, "admin-create-user"))
    updates = [
        c
        for c in _command_invocations(src, "admin-update-user-attributes")
        if "Name=email_verified,Value=true" in c
    ]
    assert len(updates) == creates, (
        f"{creates} admin-create-user call(s) but {len(updates)} matching "
        "admin-update-user-attributes repair call(s); each create path needs one "
        "so re-running the target fixes a pre-existing unverified user"
    )


def test_seed_users_writes_both_a_role_row_and_group_membership():
    """seed-users is the path that grants a role outright. The DDB row backs
    branch 2 of resolve_role and the group backs branch 1; losing either
    silently downgrades every seeded login to viewer."""
    src = _read()
    seed = src[src.index("seed_one_role()") : src.index("seed_users()")]
    assert "dynamodb put-item" in seed, "seed_one_role must write the user-roles row"
    assert "admin-add-user-to-group" in seed, (
        "seed_one_role must add the user to the matching Cognito group"
    )


def test_role_names_match_the_rbac_role_enum():
    """The seeded group and role-row names are a contract with rbac.Role: the
    backend lower-cases each and matches it against the enum, so a typo here
    resolves to viewer rather than erroring."""
    from app.api.rbac import Role

    src = _read()
    seeded = set(re.findall(r'seed_one_role\s+"(\w+)"', src))
    assert seeded == {r.value for r in Role}, (
        f"seed_one_role covers {sorted(seeded)}, but rbac.Role declares "
        f"{sorted(r.value for r in Role)}"
    )
