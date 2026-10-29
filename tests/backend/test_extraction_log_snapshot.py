# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression: the persisted job snapshot must include the log entries, so a
completed job's Extraction Log survives S3 hydration (task restart / doc-detail
panel). Without this the panel showed "Waiting for the agent to start…" forever.
"""
import app.services.extraction_log as el


def test_snapshot_includes_log_entries():
    el.start_job("doc-snap", "f.pdf")
    el.append("doc-snap", "rule", "Saved standard: [HA-D-1] X")
    el.append("doc-snap", "info", "done")
    el.mark_completed("doc-snap", 111)
    snap = el._snapshot(el._jobs["doc-snap"])
    assert "log" in snap
    assert len(snap["log"]) >= 3  # queued + rule + done + completion line
    assert snap["rules_saved"] == 111
    # entries are JSON-safe dicts, not LogEntry objects
    assert all(isinstance(e, dict) and "seq" in e for e in snap["log"])


def test_hydrate_restores_log_from_snapshot(monkeypatch):
    """A snapshot round-trips: persist -> hydrate rebuilds the log so
    get_log_since serves it after a cache miss."""
    el.start_job("doc-rt", "f.pdf")
    el.append("doc-rt", "rule", "Saved standard: [HA-D-9] Y")
    el.mark_completed("doc-rt", 5)
    import json
    payload = json.dumps(el._snapshot(el._jobs["doc-rt"])).encode()

    # Simulate a fresh task: drop from memory, serve snapshot from "S3".
    el._jobs.pop("doc-rt", None)
    class FakeStore:
        def download_document(self, key): return payload
    monkeypatch.setattr("app.services.document_store.document_store", FakeStore())

    log = el.get_log_since("doc-rt", since_seq=0)
    assert len(log) >= 2  # rule + completion line survived hydration
