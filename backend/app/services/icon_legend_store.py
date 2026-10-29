# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom on-prem icon legend.

A tenant's own architecture iconography: each entry teaches the model what a
non-standard glyph means. Two grounding layers:

  - TEXT (always applied): label + component type + a human description. Cheap,
    deterministic, auditable — the written glyph dictionary. Handles the common
    case (non-standard labels / describable shapes).
  - IMAGE (optional, per glyph): an uploaded icon crop (S3 key) embedded as a
    visual few-shot example, ONLY for glyphs flagged unlabeled/ambiguous where
    text alone can't disambiguate the pixels. Capped per prompt to avoid
    attention dilution.

Injected into BOTH extraction diagram prompts and validation diagram prompts so
extraction and validation share one glyph vocabulary (mismatched vocabularies
would make extraction understand icons validation then can't → spurious FAILs).

Mirrors references_store, plus an in-memory fallback so tests/local runs work
offline.
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

# Cap how many icon IMAGES get embedded into a single diagram prompt. The text
# legend is always fully applied; only images are capped (attention dilution +
# token cost grow with embedded images, and beyond a handful accuracy can
# regress). Env-tunable.
MAX_LEGEND_IMAGES = int(os.getenv("ICON_LEGEND_MAX_IMAGES", "6"))

# Valid node types mirror the topology extractor's closed enum so a legend
# entry maps a custom glyph onto a type the graph model already understands.
VALID_ICON_TYPES = {
    "server", "database", "load_balancer", "datacenter", "network",
    "storage", "service", "firewall", "gateway", "other",
}


def _table_name() -> str:
    return os.getenv(
        "ICON_LEGEND_TABLE",
        f"{os.getenv('DYNAMODB_TABLE_PREFIX', '')}-icon-legend",
    )


class IconLegendStore:
    def __init__(self) -> None:
        self._available = True
        self._mem: dict[str, dict] = {}
        try:
            self._client = boto3.resource(
                "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
            )
            self._table = self._client.Table(_table_name())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"icon_legend_store: DDB init failed, in-memory only: {e}")
            self._available = False

    # ---- mutation ----------------------------------------------------

    def create(
        self,
        *,
        label: str,
        icon_type: str,
        description: str = "",
        hosting_scope: str = "on_prem",
        image_s3_key: Optional[str] = None,
        created_by: str = "",
        status: str = "active",
        source_kind: str = "text",
        ai_generated: bool = False,
        ai_model: str = "",
        confidence: str = "",
        bulk_batch_id: Optional[str] = None,
        image_filename: str = "",
    ) -> dict:
        icon_id = f"ICON-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now(timezone.utc).isoformat()
        it = (icon_type or "other").strip().lower()
        if it not in VALID_ICON_TYPES:
            it = "other"
        sk = source_kind if source_kind in ("text", "image") else "text"
        # FAIL-CLOSED on the trust-critical field: an unknown/typo'd status must
        # NOT silently become active (which would inject an unapproved entry
        # into the validator). Default anything unrecognised to draft so it
        # stays behind the human approval gate.
        st = status if status in ("active", "draft") else "draft"
        item = {
            "id": icon_id,
            # record_type separates icon rows from bulk-job rows that share this
            # table (see put_bulk_job). list_all returns only icons.
            "record_type": "icon",
            "label": label,
            "icon_type": it,
            "description": description or "",
            "hosting_scope": hosting_scope or "on_prem",
            "status": st,
            # source_kind distinguishes text-authored vs image-derived entries;
            # the UI + injection treat them the same at run time, but authoring
            # + provenance differ. ai_generated/ai_model record whether a model
            # produced the definition (audit for the regulated context).
            "source_kind": sk,
            "ai_generated": bool(ai_generated),
            "created_by": created_by or "",
            "created_at": now,
            "updated_at": now,
        }
        if image_s3_key:
            item["image_s3_key"] = image_s3_key
        if ai_model:
            item["ai_model"] = ai_model
        if confidence:
            item["confidence"] = confidence
        if bulk_batch_id:
            item["bulk_batch_id"] = bulk_batch_id
        # Declared image filename from a bulk text row. Kept so a later
        # multi-file upload can pair each file to the row that asked for it;
        # matching on the label instead would break as soon as a curator edits
        # the label during review.
        if image_filename:
            item["image_filename"] = image_filename
        self._put(item)
        return item

    def update(self, icon_id: str, fields: dict) -> Optional[dict]:
        existing = self.get(icon_id)
        if existing is None:
            return None
        clean = {k: v for k, v in fields.items() if v is not None}
        if "icon_type" in clean:
            t = str(clean["icon_type"]).strip().lower()
            clean["icon_type"] = t if t in VALID_ICON_TYPES else "other"
        # Clamp status here too (fail-closed): update() is an alternate write
        # path, so an unrecognised status must never land as active. Approval
        # still flows through the dedicated /approve route for the audit trail.
        if "status" in clean:
            clean["status"] = clean["status"] if clean["status"] in ("active", "draft") else "draft"
        merged = {**existing, **clean, "updated_at": datetime.now(timezone.utc).isoformat()}
        self._put(merged)
        return merged

    def delete(self, icon_id: str) -> bool:
        if not self._available:
            return self._mem.pop(icon_id, None) is not None
        try:
            self._table.delete_item(Key={"id": icon_id})
            return True
        except ClientError as e:
            logger.error(f"icon_legend_store delete failed for {icon_id}: {e}")
            return False

    def _put(self, item: dict) -> None:
        if not self._available:
            self._mem[item["id"]] = item
            return
        try:
            self._table.put_item(Item=item)
        except ClientError as e:
            logger.error(f"icon_legend_store put failed: {e}")
            raise

    # ---- bulk-job state (persisted, so poll survives multi-task autoscale) --
    #
    # The bulk-image interpret job's progress can't live in a per-process dict:
    # the service autoscales to several ECS tasks, so the UI's poll request may
    # land on a task that never registered the job. We persist job state as a
    # row in this same table (record_type="bulk_job", id="JOB#<job_id>") so any
    # task can read it. Only the owning task's worker threads write, serialized
    # by the route's lock, so a get-modify-write on the counters is safe enough
    # for this low-concurrency tool; DDB atomic ADD would be stronger but the
    # in-memory fallback (tests/offline) needs the same code path.

    @staticmethod
    def _job_key(job_id: str) -> str:
        return f"JOB#{job_id}"

    # Job rows self-expire so they can't accumulate unboundedly and bloat the
    # table (which is full-scanned on the diagram-prompt hot path). A day is far
    # longer than any interpret job runs; the UI only polls for minutes.
    _JOB_TTL_SECONDS = 24 * 60 * 60

    def put_bulk_job(self, job: dict) -> None:
        ttl = int(datetime.now(timezone.utc).timestamp()) + self._JOB_TTL_SECONDS
        item = {
            **job,
            "id": self._job_key(job["job_id"]),
            "record_type": "bulk_job",
            "ttl": ttl,  # DynamoDB TTL attribute — reaps stale job rows
        }
        self._put(item)

    def get_bulk_job(self, job_id: str) -> Optional[dict]:
        row = self.get(self._job_key(job_id))
        if not row:
            return None
        # Strip storage-internal keys; return the job shape the UI expects.
        job = {k: v for k, v in row.items()
               if k not in ("id", "record_type", "ttl")}
        # DDB returns numbers as Decimal; coerce counters to int for a clean
        # JSON shape (the in-memory path already has ints).
        for k in ("total", "done", "failed"):
            if k in job:
                try:
                    job[k] = int(job[k])
                except (TypeError, ValueError):
                    pass
        return job

    # ---- read --------------------------------------------------------

    def get(self, icon_id: str) -> Optional[dict]:
        if not self._available:
            return self._mem.get(icon_id)
        try:
            resp = self._table.get_item(Key={"id": icon_id})
            return resp.get("Item")
        except ClientError as e:
            logger.error(f"icon_legend_store get failed for {icon_id}: {e}")
            return None

    def list_all(
        self,
        *,
        status: Optional[str] = "active",
        hosting_scope: Optional[str] = None,
        bulk_batch_id: Optional[str] = None,
    ) -> list[dict]:
        if not self._available:
            items = list(self._mem.values())
        else:
            try:
                # Paginate: a single scan() returns at most 1MB, so without
                # following LastEvaluatedKey a growing table would SILENTLY drop
                # rows — and this runs on the diagram-prompt hot path, so a
                # dropped row = an approved icon that stops being injected (a
                # trust regression with no error). Filter out non-icon rows
                # (bulk-job state shares the table) server-side so they don't
                # consume page space.
                items = scan_all(
                    self._table,
                    FilterExpression=(
                        "attribute_not_exists(record_type) OR record_type = :ic"
                    ),
                    ExpressionAttributeValues={":ic": "icon"},
                )
            except ClientError as e:
                logger.error(f"icon_legend_store list_all failed: {e}")
                return []
        # Exclude non-icon rows (bulk-job state shares the table). Redundant with
        # the server-side filter above, but keeps the in-memory path correct.
        items = [i for i in items if (i.get("record_type") or "icon") == "icon"]
        if status is not None:
            items = [i for i in items if (i.get("status") or "active") == status]
        if hosting_scope is not None:
            items = [
                i for i in items
                if (i.get("hosting_scope") or "on_prem") in (hosting_scope, "both")
            ]
        if bulk_batch_id is not None:
            items = [i for i in items if i.get("bulk_batch_id") == bulk_batch_id]
        # Newest first for stable review-queue ordering.
        items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
        return items


icon_legend_store = IconLegendStore()
