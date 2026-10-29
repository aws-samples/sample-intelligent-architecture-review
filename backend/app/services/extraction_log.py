# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Live-log registry for extraction jobs — in-memory while running,
S3-persisted on completion.

Owns the `_jobs` dict that the /extraction-status endpoint polls,
plus a Strands-compatible callback handler that filters the agent's
event stream down to the user-relevant lines:

  - Tool calls (save_standard, get_document_text, etc.)
  - Tool results (especially "Saved standard: [HA-D-X] ...")
  - Reasoning summaries (the agent's narrative text between tool calls,
    truncated to 200 chars per chunk so a thinking dump doesn't blow
    the log)

Persistence model:
  - In-flight jobs live only in `_jobs` (process memory). High-frequency
    appends would be expensive to round-trip through S3.
  - Terminal jobs (status=completed/failed) get serialized to
    documents/{id}/extraction_log.json on the documents bucket.
    get_job/get_log_since lazy-load from S3 on a cache miss so the
    doc-detail Extraction Log panel survives ECS task restarts.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Per-process registry. Keyed by document_id (one in-flight job per
# document at a time — re-uploads mint a new doc-{uuid}). Each entry:
#   {
#     "document_id": str,
#     "filename": str,
#     "status": "starting" | "extracting" | "completed" | "failed",
#     "started_at": iso8601 str,
#     "completed_at": Optional[iso8601 str],
#     "error": Optional[str],
#     "log": list[LogEntry] — append-only, capped at MAX_LOG_LINES,
#     "log_seq": int — monotonic counter; clients poll
#                       /extraction-status?since_seq=N for the tail.
#     "rules_saved": int — derived count for the banner.
#   }
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

MAX_LOG_LINES = 500
MAX_TEXT_CHUNK = 200

# Statuses that mean a run is still going. Named once so the start-guard and the
# approve/reject/set-scope guard agree on what "in flight" means.
IN_FLIGHT_STATES = ("starting", "extracting", "enriching")


def _s3_key(document_id: str) -> str:
    return f"documents/{document_id}/extraction_log.json"


def _persist_to_s3(document_id: str) -> None:
    """Snapshot a terminal job to S3. Best-effort — silently logs and
    swallows failures so the user-facing flow isn't blocked by an S3
    hiccup."""
    try:
        from app.services.document_store import document_store
    except Exception as e:
        logger.warning(f"extraction_log persist: doc store unavailable: {e}")
        return

    with _jobs_lock:
        job = _jobs.get(document_id)
        if not job:
            return
        payload = _snapshot(job)
        # _snapshot already converts log entries to JSON-safe dicts
        # via to_json(); same shape the API serves to the frontend.

    try:
        body = json.dumps(payload).encode("utf-8")
        document_store.upload_document(
            body, _s3_key(document_id), content_type="application/json"
        )
        logger.info(
            f"extraction_log: persisted terminal log for {document_id} "
            f"(status={payload.get('status')})"
        )
    except Exception as e:
        logger.warning(f"extraction_log persist failed for {document_id}: {e}")


def _hydrate_from_s3(document_id: str) -> Optional[dict]:
    """Read a previously-persisted terminal job back into _jobs from
    S3. Returns the rehydrated job dict, or None when no snapshot
    exists. Idempotent — repeated calls just re-populate _jobs."""
    try:
        from app.services.document_store import document_store
    except Exception:
        return None

    try:
        raw = document_store.download_document(_s3_key(document_id))
    except Exception:
        return None
    if not raw:
        return None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.warning(f"extraction_log: malformed S3 snapshot for {document_id}: {e}")
        return None

    # Rebuild the in-memory shape so subsequent get_job/get_log_since
    # calls hit the local cache without re-fetching S3.
    log_entries = []
    for rec in payload.get("log", []):
        log_entries.append(
            LogEntry(
                seq=int(rec.get("seq", 0)),
                ts=float(rec.get("ts", 0.0)) if rec.get("ts") else time.time(),
                level=str(rec.get("level", "info")),
                message=str(rec.get("message", "")),
            )
        )
    job = {
        "document_id": payload.get("document_id", document_id),
        "filename": payload.get("filename", ""),
        "status": payload.get("status", "completed"),
        "started_at": payload.get("started_at", ""),
        "completed_at": payload.get("completed_at"),
        "error": payload.get("error"),
        "rules_saved": int(payload.get("rules_saved", 0) or 0),
        "log": log_entries,
        "log_seq": max((e.seq for e in log_entries), default=0),
    }
    with _jobs_lock:
        _jobs[document_id] = job
    return job


@dataclass
class LogEntry:
    seq: int
    ts: float            # epoch seconds for client-side ordering
    level: str           # "tool" | "rule" | "thought" | "info" | "error"
    message: str

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "level": self.level,
            "message": self.message,
        }


# In-flight state has to be readable from ANY task, not just the one running the
# extraction. The live log lives in this process's memory and is only persisted to
# S3 on a terminal status, so mid-run there is nothing another task can see. The
# extraction review gate depends on knowing a run is in progress — without a
# cross-task signal it fails open, and a curator can approve proposed rules while
# dedup and cross-reference wiring are still running over that same batch.
#
# So each transition also stamps the state on the document row (one attribute,
# already-existing table). Best-effort: a stamp failure must never break the run.
def _stamp_document_state(document_id: str, state: Optional[str]) -> None:
    """Record (or clear) the in-flight extraction state on the document row."""
    if not document_id:
        return
    try:
        from app.services.validation_store import validation_store
        validation_store.set_extraction_state(document_id, state)
    except Exception:  # noqa: BLE001 — telemetry must not sink an extraction
        logger.warning(
            "could not stamp extraction state for %s", document_id, exc_info=True
        )


def start_job(document_id: str, filename: str) -> None:
    """Initialise a fresh job entry. Overwrites any prior entry for
    the same document_id (re-uploads mint new doc-{uuid}, so this is
    rarely a real overwrite)."""
    from datetime import datetime, timezone

    with _jobs_lock:
        _jobs[document_id] = {
            "document_id": document_id,
            "filename": filename,
            "status": "starting",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "error": None,
            "log": [],
            "log_seq": 0,
            "rules_saved": 0,
        }
    _stamp_document_state(document_id, "starting")
    append(document_id, "info", f"Extraction queued for {filename}")


def mark_extracting(document_id: str) -> None:
    with _jobs_lock:
        if document_id in _jobs:
            _jobs[document_id]["status"] = "extracting"
    _stamp_document_state(document_id, "extracting")
    append(document_id, "info", "Agent invoked — reasoning over the document…")


def mark_enriching(document_id: str) -> None:
    """Text extraction is done; per-image enrichment is starting. Status flips to
    'enriching' so the UI banner shows "Enriching diagrams…" instead of jumping
    straight to "completed" while enrichment is still emitting log lines."""
    with _jobs_lock:
        if document_id in _jobs:
            _jobs[document_id]["status"] = "enriching"
    _stamp_document_state(document_id, "enriching")
    append(document_id, "info", "Text extraction complete — starting image enrichment…")


def mark_completed(document_id: str, rules_saved: int) -> None:
    from datetime import datetime, timezone

    with _jobs_lock:
        if document_id in _jobs:
            _jobs[document_id]["status"] = "completed"
            _jobs[document_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
            _jobs[document_id]["rules_saved"] = rules_saved
    _stamp_document_state(document_id, None)
    append(
        document_id,
        "info",
        f"Extraction complete — {rules_saved} rule{'s' if rules_saved != 1 else ''} saved.",
    )
    # Persist after the final append so the snapshot includes the
    # completion line.
    _persist_to_s3(document_id)


def mark_failed(document_id: str, error: str) -> None:
    from datetime import datetime, timezone

    with _jobs_lock:
        if document_id in _jobs:
            _jobs[document_id]["status"] = "failed"
            _jobs[document_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
            _jobs[document_id]["error"] = error
    _stamp_document_state(document_id, None)
    append(document_id, "error", f"Extraction failed: {error}")
    _persist_to_s3(document_id)


def append(document_id: str, level: str, message: str) -> None:
    """Add a single log entry. Thread-safe."""
    with _jobs_lock:
        job = _jobs.get(document_id)
        if job is None:
            return
        job["log_seq"] += 1
        entry = LogEntry(
            seq=job["log_seq"],
            ts=time.time(),
            level=level,
            message=message[:1000],  # hard cap so a runaway message doesn't bloat
        )
        job["log"].append(entry)
        if len(job["log"]) > MAX_LOG_LINES:
            # Drop oldest entries; keep tail. Banner only cares about
            # latest state so this is fine.
            del job["log"][: len(job["log"]) - MAX_LOG_LINES]


def get_job(document_id: str) -> Optional[dict]:
    """Return a JSON-safe snapshot of one job. Falls back to S3 for
    jobs that completed in a previous task generation (so the doc-
    detail Extraction Log panel survives ECS task restarts)."""
    with _jobs_lock:
        job = _jobs.get(document_id)
    if job is None:
        # Lazy-hydrate from S3 — populates _jobs as a side effect so
        # subsequent calls hit the in-memory cache.
        job = _hydrate_from_s3(document_id)
        if job is None:
            return None
    with _jobs_lock:
        # Re-fetch under the lock in case _hydrate_from_s3 updated it
        # while we were dropping the lock above.
        live = _jobs.get(document_id, job)
        return _snapshot(live)


def get_active_jobs() -> list[dict]:
    """All in-flight jobs (status in {starting, extracting, enriching})."""
    with _jobs_lock:
        return [
            _snapshot(j)
            for j in _jobs.values()
            if j["status"] in IN_FLIGHT_STATES
        ]


def is_in_flight(document_id: str) -> bool:
    """True when an extraction run for this document is still going.

    Checks two sources, because a job's live log lives in ONE task's memory:
      1. the same-task live log (authoritative when this task owns the run);
      2. the extraction_state stamped on the document row, readable from ANY
         task (with a staleness window, so a task killed mid-run doesn't lock
         the document forever).

    Shared by the approve/reject/set-scope gate AND the extraction start-guard
    so both agree on "in flight". Fails OPEN (returns False) if neither source
    resolves — an untracked run must not permanently block a new one, matching
    the existing gate's philosophy.
    """
    if not document_id:
        return False
    # get_job is the canonical accessor: same-task live job, else an S3-hydrated
    # snapshot (which is always terminal, so it reads as not-in-flight). Called
    # WITHOUT holding _jobs_lock — get_job takes it, and threading.Lock is not
    # reentrant.
    try:
        job = get_job(document_id)
        if job and job.get("status") in IN_FLIGHT_STATES:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.services.validation_store import validation_store
        return validation_store.get_extraction_state(document_id) in IN_FLIGHT_STATES
    except Exception:  # noqa: BLE001 — a read failure must not fabricate "busy"
        return False


def get_completed_jobs(limit: int = 5) -> list[dict]:
    with _jobs_lock:
        comp = [_snapshot(j) for j in _jobs.values() if j["status"] == "completed"]
    return comp[-limit:]


def get_failed_jobs(limit: int = 3) -> list[dict]:
    with _jobs_lock:
        fail = [_snapshot(j) for j in _jobs.values() if j["status"] == "failed"]
    return fail[-limit:]


def get_log_since(document_id: str, since_seq: int = 0) -> list[dict]:
    """Tail-poll the log. Returns entries with seq > since_seq.
    Falls back to S3-persisted log on cache miss so a fresh ECS
    task can serve the historical log for a previously-completed
    extraction."""
    with _jobs_lock:
        job = _jobs.get(document_id)
    if job is None:
        job = _hydrate_from_s3(document_id)
        if job is None:
            return []
    with _jobs_lock:
        live = _jobs.get(document_id, job)
        return [e.to_json() for e in live["log"] if e.seq > since_seq]


def _snapshot(job: dict) -> dict:
    """JSON-safe view of a job (no LogEntry objects in the result).

    Includes the serialized log entries so the S3-persisted snapshot can
    rehydrate the full log after a task restart or on the doc-detail panel
    for a completed job. Without `log` here, _hydrate_from_s3 restored an
    empty log and the panel showed "Waiting for the agent to start…" forever
    even though extraction had completed.
    """
    return {
        "document_id": job["document_id"],
        "filename": job["filename"],
        "status": job["status"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
        "error": job["error"],
        "rules_saved": job["rules_saved"],
        "log_seq": job["log_seq"],
        "log": [e.to_json() for e in job.get("log", [])],
    }


# ---------------------------------------------------------------------------
# Strands callback handler — filters the event stream down to the
# user-relevant lines per spec ("tool calls + final reasoning summaries
# + when a rule is written").
# ---------------------------------------------------------------------------


class LiveLogCallbackHandler:
    """Captures Strands events into the live-log registry.

    Filters per spec:
      - tool calls → one log line per unique tool invocation
      - rule writes → "Saved standard: [HA-D-X] ..." lines from
        save_standard's return value
      - reasoning text → emitted as "thought" entries, truncated to
        MAX_TEXT_CHUNK so a long deliberation doesn't dominate the log

    Shaped after strands.handlers.callback_handler.PrintingCallbackHandler.
    """

    def __init__(self, document_id: str):
        self.document_id = document_id
        self.previous_tool_use = None
        self.tool_count = 0
        # Buffer model text between tool calls so a "thought" line is
        # one coherent thing, not 200 token-stream fragments.
        self._text_buffer = ""

    def __call__(self, **kwargs) -> None:
        try:
            self._dispatch(**kwargs)
        except Exception:
            # NEVER let the callback crash the agent run.
            logger.exception("LiveLogCallbackHandler error — swallowed")

    def _flush_text(self) -> None:
        text = self._text_buffer.strip()
        self._text_buffer = ""
        if not text:
            return
        # Compact: collapse whitespace, truncate.
        compact = " ".join(text.split())
        if len(compact) > MAX_TEXT_CHUNK:
            compact = compact[: MAX_TEXT_CHUNK - 1] + "…"
        append(self.document_id, "thought", compact)

    def _dispatch(self, **kwargs) -> None:
        data = kwargs.get("data") or ""
        complete = kwargs.get("complete", False)
        current_tool_use = kwargs.get("current_tool_use") or {}

        if data:
            self._text_buffer += data
            # Flush periodically so the user sees model thinking in
            # real time, not just at message boundaries. Per-turn
            # reasoning between save_standard calls can run for 30+
            # seconds — without this, the live log goes silent during
            # the entire output stream and only catches up when the
            # turn completes. Flush every ~60 chars of buffered text
            # so the user sees a steady cadence of THINK lines.
            FLUSH_INTERVAL = 80
            if complete or len(self._text_buffer) >= FLUSH_INTERVAL:
                self._flush_text()

        # Tool-use start: emit one "tool" line per unique invocation.
        # Note: "rule" entries (the "Saved standard: [HA-D-X] ..." log
        # lines) are emitted directly from save_standard's tool body,
        # NOT from this callback. That keeps the canonical "a rule
        # was actually saved" signal source-of-truth at the tool, and
        # avoids the brittleness of parsing Strands' message events.
        if current_tool_use and current_tool_use.get("name"):
            tool_name = current_tool_use.get("name", "unknown")
            if self.previous_tool_use != current_tool_use:
                # Flush any pending reasoning text BEFORE the tool line
                # so order in the UI matches the model's actual sequence.
                self._flush_text()
                self.previous_tool_use = current_tool_use
                self.tool_count += 1
                tool_input = current_tool_use.get("input") or {}
                hint = ""
                if isinstance(tool_input, dict):
                    name = tool_input.get("name") or tool_input.get("filename")
                    if name:
                        hint = f" — {name}"
                # save_standard generates "rule" entries; skip
                # generating a redundant "tool" line for it.
                if tool_name != "save_standard":
                    append(
                        self.document_id,
                        "tool",
                        f"#{self.tool_count} {tool_name}{hint}",
                    )