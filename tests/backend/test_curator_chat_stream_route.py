# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Route-level tests for POST /api/standards/chat/stream.

Verifies the SSE contract and the durability wiring without a live
Bedrock or DynamoDB:

  - the ``job`` frame is emitted FIRST (client remembers it to resume),
  - ``proposal_progress`` / ``proposals`` frames are forwarded,
  - the stream ends with ``done``,
  - the durable job record is COMPLETED from the pump thread with the
    final reply + proposals (so a mid-stream refresh can recover them).

chat_stream is replaced with a scripted generator so the route logic is
tested in isolation from the model.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _parse_sse(body: str):
    """Parse an SSE body into a list of (event, data_str) tuples."""
    frames = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        event = "message"
        data = ""
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data += line[len("data:"):].strip()
        frames.append((event, data))
    return frames


@pytest.fixture
def route_harness():
    """Override auth + patch the job store and chat_stream generator."""
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {"sub": "test-user"}

    fake_store = MagicMock()
    fake_store.create.return_value = "chat-test-123"

    scripted = [
        ("thinking", "considering the ask"),
        ("delta", "Here is a rule. "),
        ("proposal_progress", 1),
        ("proposals", [{"id": "HA-G-ABC", "name": "R", "status": "proposed"}]),
        ("reply", "Here is a rule."),
    ]

    def fake_stream(messages, hosting_scope=None):
        yield from scripted

    try:
        with patch(
            "app.services.chat_jobs_store.chat_jobs_store", fake_store
        ), patch(
            "app.api.routes.standards.rules_chat_stream", fake_stream
        ):
            yield fake_store
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_stream_route_emits_job_first_and_done_last(route_harness):
    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/api/standards/chat/stream",
        json={"messages": [{"role": "user", "content": "add a rule"}], "hosting_scope": "both"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(resp.text)
    events = [e for e, _ in frames]

    # job frame is first so the client can remember it before any tokens.
    assert events[0] == "job"
    assert "chat-test-123" in frames[0][1]
    # done frame is last.
    assert events[-1] == "done"
    # the middle carries the forwarded events.
    assert "thinking" in events
    assert "delta" in events
    assert "proposal_progress" in events
    assert "proposals" in events
    # the internal "reply" tuple is consumed for persistence, not emitted.
    assert "reply" not in events


def test_stream_route_persists_completed_job(route_harness):
    from app.main import app

    client = TestClient(app)
    client.post(
        "/api/standards/chat/stream",
        json={"messages": [{"role": "user", "content": "add a rule"}], "hosting_scope": "both"},
    )

    # Durable record created + marked running + completed with reply/proposals.
    route_harness.create.assert_called_once()
    route_harness.mark_running.assert_called_once_with("chat-test-123")
    route_harness.complete.assert_called_once()
    _, kwargs = route_harness.complete.call_args
    assert kwargs["reply"] == "Here is a rule."
    assert kwargs["proposals"] == [
        {"id": "HA-G-ABC", "name": "R", "status": "proposed"}
    ]
    route_harness.fail.assert_not_called()


def test_stream_route_marks_failed_on_error(route_harness):
    from app.main import app
    import app.api.routes.standards as standards_mod

    def boom_stream(messages, hosting_scope=None):
        yield ("delta", "partial ")
        yield ("error", "bedrock exploded")

    with patch.object(standards_mod, "rules_chat_stream", boom_stream):
        client = TestClient(app)
        resp = client.post(
            "/api/standards/chat/stream",
            json={"messages": [{"role": "user", "content": "x"}], "hosting_scope": "both"},
        )

    frames = _parse_sse(resp.text)
    events = [e for e, _ in frames]
    assert "error" in events
    # job is marked failed, not completed.
    route_harness.fail.assert_called_once()
    route_harness.complete.assert_not_called()
