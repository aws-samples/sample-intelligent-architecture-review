# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DDB-backed store for user→role assignments (RBAC).

One row per user, keyed on Cognito ``sub``. The row also carries ``email`` for
human-readable admin listing. Thin DynamoDB wrapper matching chat_jobs_store /
references_store; falls back to an in-memory dict when DynamoDB is unavailable
(``_available = False``) so tests and local runs work offline.

Role resolution itself lives in ``app.api.rbac`` — this module only persists
the assignments an admin makes.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from app.services.ddb import scan_all

logger = logging.getLogger(__name__)


def _table_name() -> str:
    # DYNAMODB_TABLE_PREFIX is a required setting (config fails fast if unset),
    # so an empty prefix means misconfigured, not a silent wrong table.
    return os.getenv(
        "USER_ROLES_TABLE",
        f"{os.getenv('DYNAMODB_TABLE_PREFIX', '')}-user-roles",
    )


class UserRolesStore:
    def __init__(self) -> None:
        self._available = True
        self._mem: dict[str, dict] = {}
        try:
            self._client = boto3.resource(
                "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
            )
            self._table = self._client.Table(_table_name())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"user_roles_store: DDB init failed, in-memory only: {e}")
            self._available = False

    def get_role(self, sub: str) -> Optional[str]:
        """Return the assigned role for a Cognito sub, or None if unassigned.

        Never raises — a store failure returns None so the caller (rbac
        resolve_role) falls back to its safe default (viewer)."""
        if not sub:
            return None
        if not self._available:
            row = self._mem.get(sub)
            return row.get("role") if row else None
        try:
            resp = self._table.get_item(Key={"sub": sub})
            item = resp.get("Item")
            return item.get("role") if item else None
        except ClientError as e:
            logger.warning(f"user_roles_store get_role failed for {sub}: {e}")
            return None

    def set_role(self, sub: str, role: str, email: str = "") -> dict:
        """Assign (or reassign) a role to a user. Returns the stored row."""
        now = datetime.now(timezone.utc).isoformat()
        item = {"sub": sub, "role": role, "email": email, "updated_at": now}
        if not self._available:
            self._mem[sub] = item
            return item
        try:
            self._table.put_item(Item=item)
        except ClientError as e:
            logger.error(f"user_roles_store set_role failed for {sub}: {e}")
            raise
        return item

    def remove_role(self, sub: str) -> bool:
        """Delete a user's role assignment. Returns True if a row was removed."""
        if not self._available:
            return self._mem.pop(sub, None) is not None
        try:
            self._table.delete_item(Key={"sub": sub})
            return True
        except ClientError as e:
            logger.error(f"user_roles_store remove_role failed for {sub}: {e}")
            return False

    def list_all(self) -> list[dict]:
        """All role assignments (admin UI). Best-effort — [] on failure."""
        if not self._available:
            return list(self._mem.values())
        try:
            return scan_all(self._table)
        except ClientError as e:
            logger.warning(f"user_roles_store list_all failed: {e}")
            return []


user_roles_store = UserRolesStore()
