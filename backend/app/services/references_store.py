# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DDB-backed store for References.

Mirrors the standards-side persistence in validation_store but in
its own table. Keeping References fully separate from rules avoids
mixing two semantically different things in one collection — every
list/filter/curate flow already knows the rules table; References
are queried only by the validator-prompt-builder and the new
References UI tab.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from app.services.ddb import scan_all

logger = logging.getLogger(__name__)


def _table_name() -> str:
    # Env var wins; otherwise derive from the shared table prefix so a
    # renamed env / fresh account deploy resolves correctly. No hardcoded
    # project fallback — DYNAMODB_TABLE_PREFIX is required (see
    # config.Settings.validate_startup_config). Mirrors
    # project_store._table_name().
    return os.getenv(
        "REFERENCES_TABLE",
        f"{os.getenv('DYNAMODB_TABLE_PREFIX', '')}-references",
    )


class ReferencesStore:
    """Thin DynamoDB wrapper. No in-memory cache today; the table is
    small and validator prompt-build is one-shot per validation.
    """

    def __init__(self) -> None:
        self._client = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        self._table = self._client.Table(_table_name())

    # ---- mutation -----------------------------------------------------

    def create(
        self,
        *,
        name: str,
        body: str,
        applies_to_tiers: list[int],
        layer: Optional[str],
        hosting_scope: str,
        source_document: Optional[str] = None,
        page_reference: Optional[str] = None,
        source_quote: Optional[str] = None,
        status: str = "active",
    ) -> dict:
        ref_id = f"HA-R-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "id": ref_id,
            "name": name,
            "body": body,
            "applies_to_tiers": [int(t) for t in (applies_to_tiers or [])],
            "hosting_scope": hosting_scope,
            "status": status,
            "created_at": now,
            "updated_at": now,
        }
        if layer:
            item["layer"] = layer
        if source_document:
            item["source_document"] = source_document
        if page_reference:
            item["page_reference"] = page_reference
        if source_quote:
            item["source_quote"] = source_quote
        try:
            self._table.put_item(Item=item)
        except ClientError as e:
            logger.error(f"references_store create failed: {e}")
            raise
        return item

    def update(self, ref_id: str, fields: dict) -> Optional[dict]:
        """Patch a Reference. Skips no-ops (empty fields)."""
        if not fields:
            return self.get(ref_id)
        existing = self.get(ref_id)
        if existing is None:
            return None
        merged = {**existing, **{k: v for k, v in fields.items() if v is not None}}
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            self._table.put_item(Item=merged)
        except ClientError as e:
            logger.error(f"references_store update failed for {ref_id}: {e}")
            raise
        return merged

    def delete(self, ref_id: str) -> bool:
        try:
            self._table.delete_item(Key={"id": ref_id})
            return True
        except ClientError as e:
            logger.error(f"references_store delete failed for {ref_id}: {e}")
            return False

    # ---- read --------------------------------------------------------

    def get(self, ref_id: str) -> Optional[dict]:
        try:
            resp = self._table.get_item(Key={"id": ref_id})
        except ClientError as e:
            logger.error(f"references_store get failed for {ref_id}: {e}")
            return None
        item = resp.get("Item")
        if not item:
            return None
        return _normalize(item)

    def list_all(
        self,
        *,
        status: Optional[str] = "active",
        hosting_scope: Optional[str] = None,
    ) -> list[dict]:
        try:
            rows = scan_all(self._table)
        except ClientError as e:
            logger.error(f"references_store list_all failed: {e}")
            return []
        items = [_normalize(r) for r in rows]
        if status is not None:
            items = [i for i in items if (i.get("status") or "active") == status]
        if hosting_scope is not None:
            items = [
                i
                for i in items
                if (i.get("hosting_scope") or "both") == hosting_scope
            ]
        return items

    def matching_rule_scope(
        self,
        *,
        tiers: list[int],
        layer: Optional[str],
        hosting_scope: str,
    ) -> list[dict]:
        """Fetch References whose scope tuple covers the
        rule scope passed in. Used by parallel_validator at prompt-
        build time to attach scope-relevant context (e.g. the v2
        doc's DC-peer table).

        Match rules:
          tiers: ANY tier in the Reference's applies_to_tiers
            intersects the rule's tiers (universal Reference covers
            all rules).
          layer: Reference layer is None (any) OR equals rule layer.
          hosting_scope: Reference scope == rule scope OR Reference
            scope == "both" (covers either side).
        """
        out: list[dict] = []
        rule_tiers = {int(t) for t in (tiers or [])}
        for ref in self.list_all(status="active"):
            ref_tiers = {int(t) for t in (ref.get("applies_to_tiers") or [])}
            if rule_tiers and ref_tiers and not (rule_tiers & ref_tiers):
                continue
            ref_layer = ref.get("layer")
            if layer and ref_layer and ref_layer != layer:
                continue
            ref_scope = ref.get("hosting_scope") or "both"
            if ref_scope != "both" and ref_scope != hosting_scope:
                continue
            out.append(ref)
        return out


def _normalize(item: dict) -> dict:
    """DynamoDB returns Decimal for numeric attributes; coerce to int.
    Returns a plain JSON-friendly dict the route can return as-is."""
    out = dict(item)
    if "applies_to_tiers" in out and out["applies_to_tiers"]:
        out["applies_to_tiers"] = [int(t) for t in out["applies_to_tiers"]]
    return out


# Singleton — every call site uses the same boto3 client.
references_store = ReferencesStore()
