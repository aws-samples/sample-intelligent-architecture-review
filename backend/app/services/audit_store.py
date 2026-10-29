# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Append-only audit-event store — "who did what, when".

One row per mutating action (document upload, rule approve/reject/edit,
validation trigger, extraction trigger, role change, ...). Append-only by
convention: this store never updates or deletes rows.

Schema (matches infrastructure/modules/dynamodb audit_events table):
  event_id    (PK)  — "evt-<uuid>"
  entity_key  (GSI1 hash) — "<entity_type>#<entity_id>"  (per-entity history)
  actor_sub   (GSI2 hash) — Cognito sub of the actor      (per-user activity)
  ts          (GSI range) — ISO8601 UTC timestamp
  action      — dotted verb, e.g. "document.upload", "standard.approve"
  entity_type — "document" | "standard" | "validation" | "project" | ...
  entity_id   — the affected entity's id
  actor_email — human-readable actor email (may be "")
  actor_role  — resolved role at action time (may be "")
  detail      — optional map (before/after, batch_id, extra context)

Thin DynamoDB wrapper matching user_roles_store / references_store; falls back
to an in-memory list when DynamoDB is unavailable so tests/local runs work.
Recording is best-effort: a failure here must NEVER break the mutation it
audits (call sites wrap in try/except, and this module also swallows).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def _table_name() -> str:
    return os.getenv(
        "AUDIT_EVENTS_TABLE",
        f"{os.getenv('DYNAMODB_TABLE_PREFIX', '')}-audit-events",
    )


def _entity_key(entity_type: str, entity_id: str) -> str:
    return f"{entity_type}#{entity_id}"


class AuditStore:
    def __init__(self) -> None:
        self._available = True
        self._mem: list[dict] = []
        try:
            self._client = boto3.resource(
                "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
            )
            self._table = self._client.Table(_table_name())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"audit_store: DDB init failed, in-memory only: {e}")
            self._available = False

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        actor_sub: str = "",
        actor_email: str = "",
        actor_role: str = "",
        detail: Optional[dict] = None,
    ) -> Optional[dict]:
        """Append one audit event. Best-effort: returns the stored row, or
        None on failure (never raises — auditing must not break the action).

        actor_sub falls back to a sentinel so the GSI2 key is always present
        even for unattributed/system actions.
        """
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "event_id": f"evt-{uuid.uuid4().hex}",
            "entity_key": _entity_key(entity_type, entity_id),
            "actor_sub": actor_sub or "unknown",
            "ts": now,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor_email": actor_email or "",
            "actor_role": actor_role or "",
        }
        if detail:
            item["detail"] = detail
        try:
            if not self._available:
                self._mem.append(item)
            else:
                self._table.put_item(Item=item)
            return item
        except ClientError as e:
            logger.warning(f"audit_store record failed ({action} {entity_type}/{entity_id}): {e}")
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"audit_store record error: {e}")
            return None

    def record_from_claims(
        self,
        claims: Optional[dict],
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        detail: Optional[dict] = None,
    ) -> Optional[dict]:
        """Convenience: extract actor sub/email/role from a Cognito claims
        dict (as returned by get_current_user / require_role) and record."""
        claims = claims or {}
        return self.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_sub=str(claims.get("sub", "") or ""),
            actor_email=str(claims.get("email", "") or ""),
            actor_role=str(claims.get("_role", "") or ""),
            detail=detail,
        )

    def list_for_entity(
        self, entity_type: str, entity_id: str, limit: int = 100
    ) -> list[dict]:
        """History for one entity, newest first. Best-effort — [] on failure."""
        key = _entity_key(entity_type, entity_id)
        if not self._available:
            rows = [r for r in self._mem if r.get("entity_key") == key]
            return sorted(rows, key=lambda r: r.get("ts", ""), reverse=True)[:limit]
        try:
            from boto3.dynamodb.conditions import Key

            resp = self._table.query(
                IndexName="entity_index",
                KeyConditionExpression=Key("entity_key").eq(key),
                ScanIndexForward=False,  # newest first
                Limit=limit,
            )
            return resp.get("Items", [])
        except ClientError as e:
            logger.warning(f"audit_store list_for_entity failed for {key}: {e}")
            return []

    def list_for_actor(self, actor_sub: str, limit: int = 100) -> list[dict]:
        """Activity for one actor, newest first. Best-effort — [] on failure."""
        if not actor_sub:
            return []
        if not self._available:
            rows = [r for r in self._mem if r.get("actor_sub") == actor_sub]
            return sorted(rows, key=lambda r: r.get("ts", ""), reverse=True)[:limit]
        try:
            from boto3.dynamodb.conditions import Key

            resp = self._table.query(
                IndexName="actor_index",
                KeyConditionExpression=Key("actor_sub").eq(actor_sub),
                ScanIndexForward=False,
                Limit=limit,
            )
            return resp.get("Items", [])
        except ClientError as e:
            logger.warning(f"audit_store list_for_actor failed for {actor_sub}: {e}")
            return []


audit_store = AuditStore()
