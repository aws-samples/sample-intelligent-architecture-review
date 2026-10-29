# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Ask-Documents: the query routes and the SSE streaming callback.

At 18% this was the least-covered route module. Two distinct paths live here:
per-document queries answered by a Strands agent, and cross-document queries
answered by the Bedrock Knowledge Base. Their error handling differs on purpose
(a transient KB failure is a 503 the client should retry; anything else is a
500), and that distinction was untested.

The streaming callback runs inside the agent's executor thread and is documented
as never raising into the agent loop — a crash there would kill a run that was
otherwise fine.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.models.document import Document, DocumentStatus, DocumentType
from app.services.vector_store import TransientKBError


def _doc(status=DocumentStatus.PARSED) -> Document:
    return Document(
        document_id="doc-1",
        filename="sdd.pdf",
        s3_key="k",
        document_type=DocumentType.SDD,
        status=status,
        hosting_scope="both",
    )


class _Store:
    def __init__(self, doc=None):
        self.doc = doc

    def get_document(self, document_id):
        return self.doc


class _Vector:
    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error
        self.calls: list[dict] = []

    def retrieve_and_generate(self, query, model_arn):
        self.calls.append({"query": query, "model_arn": model_arn})
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def client(monkeypatch):
    """A TestClient with auth, both stores, and STS stubbed out."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.api.dependencies import get_validation_store, get_vector_store

    def _make(doc=None, kb_result=None, kb_error=None, agent=None):
        store = _Store(doc)
        vector = _Vector(kb_result, kb_error)

        app.dependency_overrides[get_current_user] = lambda: {
            "sub": "s", "email": "u@x.com"
        }
        app.dependency_overrides[get_validation_store] = lambda: store
        app.dependency_overrides[get_vector_store] = lambda: vector

        # The KB path resolves the account id to build an inference-profile ARN.
        import boto3

        class _STS:
            def get_caller_identity(self):
                return {"Account": "123456789012"}

        monkeypatch.setattr(boto3, "client", lambda *a, **kw: _STS())

        if agent is not None:
            import app.agents.query_agent as qa

            monkeypatch.setattr(qa, "create_query_agent", lambda *a, **kw: agent)

        return TestClient(app), vector

    yield _make

    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_validation_store, None)
    app.dependency_overrides.pop(get_vector_store, None)


class TestPerDocumentQuery:
    def test_answers_from_the_agent(self, client):
        c, _ = client(doc=_doc(), agent=lambda prompt: "Two datacentres, active-active.")
        resp = c.post("/api/queries", json={"question": "How many DCs?", "document_id": "doc-1"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["answer"] == "Two datacentres, active-active."
        assert body["document_id"] == "doc-1"
        assert body["query_id"].startswith("qry-")
        assert body["processing_time_ms"] >= 0

    def test_the_question_and_document_reach_the_agent(self, client):
        seen: list[str] = []

        def _agent(prompt):
            seen.append(prompt)
            return "ok"

        c, _ = client(doc=_doc(), agent=_agent)
        c.post("/api/queries", json={"question": "Is it multi-AZ?", "document_id": "doc-1"})
        assert "Is it multi-AZ?" in seen[0]
        assert "doc-1" in seen[0]

    def test_unknown_document_is_404(self, client):
        c, _ = client(doc=None)
        resp = c.post("/api/queries", json={"question": "q", "document_id": "nope"})
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "status", [DocumentStatus.UPLOADED, DocumentStatus.PARSING, DocumentStatus.FAILED]
    )
    def test_an_unparsed_document_is_400_not_a_bad_answer(self, client, status):
        """Querying before parsing would let the agent answer from no content."""
        c, _ = client(doc=_doc(status=status))
        resp = c.post("/api/queries", json={"question": "q", "document_id": "doc-1"})
        assert resp.status_code == 400
        assert "parsed" in resp.json()["detail"].lower()

    def test_an_agent_failure_is_a_500(self, client):
        def _boom(prompt):
            raise RuntimeError("bedrock exploded")

        c, _ = client(doc=_doc(), agent=_boom)
        resp = c.post("/api/queries", json={"question": "q", "document_id": "doc-1"})
        assert resp.status_code == 500

    def test_anonymous_callers_are_refused(self):
        """The router carries auth; no override here means no token."""
        from app.main import app

        assert TestClient(app).post(
            "/api/queries", json={"question": "q"}
        ).status_code in (401, 403)


class TestKnowledgeBaseQuery:
    def _kb(self, text="Answer text", citations=None):
        return {
            "output": {"text": text},
            "citations": citations or [],
        }

    def test_answers_across_all_documents(self, client):
        c, vector = client(kb_result=self._kb("Three regions."))
        resp = c.post("/api/queries", json={"question": "Which regions?"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["answer"] == "Three regions."
        assert body["document_id"] == "all"
        assert vector.calls[0]["query"] == "Which regions?"

    def test_the_model_arn_is_built_from_the_live_account(self, client):
        """Inference-profile ARNs are account-scoped, so a hardcoded account id
        would break the moment this deploys anywhere else."""
        c, vector = client(kb_result=self._kb())
        c.post("/api/queries", json={"question": "q"})
        arn = vector.calls[0]["model_arn"]
        assert arn.startswith("arn:aws:bedrock:")
        assert "123456789012" in arn
        assert "inference-profile/" in arn

    def test_citations_become_sources(self, client):
        c, _ = client(kb_result=self._kb(citations=[
            {
                "retrievedReferences": [
                    {
                        "location": {"s3Location": {"uri": "s3://bucket/docs/sdd.pdf"}},
                        "content": {"text": "the relevant passage"},
                    }
                ]
            }
        ]))
        body = c.post("/api/queries", json={"question": "q"}).json()
        assert len(body["sources"]) == 1
        assert body["sources"][0]["section_title"] == "sdd.pdf"
        assert "relevant passage" in body["sources"][0]["excerpt"]

    def test_sources_are_capped_at_five(self, client):
        many = [
            {
                "retrievedReferences": [
                    {
                        "location": {"s3Location": {"uri": f"s3://b/d{i}.pdf"}},
                        "content": {"text": "t"},
                    }
                ]
            }
            for i in range(12)
        ]
        c, _ = client(kb_result=self._kb(citations=many))
        assert len(c.post("/api/queries", json={"question": "q"}).json()["sources"]) == 5

    def test_a_citation_without_a_location_still_parses(self, client):
        c, _ = client(kb_result=self._kb(citations=[
            {"retrievedReferences": [{"content": {"text": "no uri here"}}]}
        ]))
        body = c.post("/api/queries", json={"question": "q"}).json()
        assert body["sources"][0]["section_title"] == "Unknown"

    def test_a_missing_answer_falls_back_to_a_message(self, client):
        c, _ = client(kb_result={"citations": []})
        body = c.post("/api/queries", json={"question": "q"}).json()
        assert body["answer"] == "No answer generated."

    def test_a_transient_kb_error_is_503_so_the_client_retries(self, client):
        """503 vs 500 is the whole point: the KB is still ingesting, and the
        client should try again rather than treat it as a bug."""
        c, _ = client(kb_error=TransientKBError("KB is still syncing"))
        resp = c.post("/api/queries", json={"question": "q"})
        assert resp.status_code == 503
        assert "syncing" in resp.json()["detail"]

    def test_any_other_kb_error_is_500(self, client):
        c, _ = client(kb_error=RuntimeError("permanent failure"))
        assert c.post("/api/queries", json={"question": "q"}).status_code == 500


class TestSseFormatting:
    def test_frames_carry_an_event_and_json_data(self):
        from app.api.routes.queries import _sse

        frame = _sse("delta", {"text": "hello"})
        assert frame.startswith("event: delta\n")
        assert frame.endswith("\n\n")
        payload = frame.split("data: ", 1)[1].strip()
        assert json.loads(payload) == {"text": "hello"}

    def test_data_is_json_escaped(self):
        """A raw newline inside the payload would terminate the frame early and
        corrupt the stream."""
        from app.api.routes.queries import _sse

        frame = _sse("delta", {"text": "line one\nline two"})
        assert frame.count("\n\n") == 1
        body = json.loads(frame.split("data: ", 1)[1].strip())
        assert body["text"] == "line one\nline two"


class TestQueueCallbackHandler:
    """The callback hops events from the agent's thread onto the event loop."""

    @pytest.fixture
    def handler(self):
        from app.api.routes.queries import _QueueCallbackHandler

        loop = asyncio.new_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        h = _QueueCallbackHandler(loop, queue)
        # Run call_soon_threadsafe callbacks inline so the test stays sync.
        loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
        yield h, queue
        loop.close()

    def _drain(self, queue):
        out = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return out

    def test_reasoning_deltas_are_emitted_as_thinking(self, handler):
        h, q = handler
        h(reasoning=True, reasoningText="considering the AZ layout")
        assert self._drain(q) == [("thinking", "considering the AZ layout")]

    def test_answer_text_is_emitted_as_a_delta(self, handler):
        h, q = handler
        h(data="the answer is")
        assert self._drain(q) == [("delta", "the answer is")]

    def test_tool_calls_are_emitted_with_a_hint(self, handler):
        h, q = handler
        h(current_tool_use={"name": "kb_search", "input": {"query": "replication"}})
        events = self._drain(q)
        assert events[0][0] == "tool"
        assert "kb_search" in events[0][1]
        assert "replication" in events[0][1]

    def test_repeated_identical_tool_events_emit_once(self, handler):
        """Strands re-sends the same tool_use on every chunk; without de-dup the
        stream fills with duplicates."""
        h, q = handler
        use = {"name": "kb_search", "input": {"query": "x"}}
        h(current_tool_use=use)
        h(current_tool_use=dict(use))
        assert len([e for e in self._drain(q) if e[0] == "tool"]) == 1

    def test_a_different_tool_emits_again(self, handler):
        h, q = handler
        h(current_tool_use={"name": "kb_search", "input": {"query": "a"}})
        h(current_tool_use={"name": "get_document_text", "input": {"document_id": "d"}})
        assert len([e for e in self._drain(q) if e[0] == "tool"]) == 2

    def test_a_long_hint_is_truncated(self, handler):
        h, q = handler
        h(current_tool_use={"name": "kb_search", "input": {"query": "y" * 300}})
        event = self._drain(q)[0][1]
        assert event.count("y") == 80

    def test_a_tool_without_input_still_emits(self, handler):
        h, q = handler
        h(current_tool_use={"name": "some_tool"})
        assert self._drain(q)[0] == ("tool", "some_tool")

    def test_reasoning_short_circuits_before_other_handling(self, handler):
        """A reasoning frame must not also be emitted as answer text."""
        h, q = handler
        h(reasoning=True, reasoningText="thinking", data="should not appear")
        assert self._drain(q) == [("thinking", "thinking")]

    def test_a_callback_crash_never_reaches_the_agent(self, handler):
        """Documented contract: the agent loop must survive a bad callback."""
        h, q = handler

        class _Explodes(dict):
            def get(self, *a, **kw):
                raise RuntimeError("malformed event")

        h(current_tool_use=_Explodes())  # must not raise

    def test_empty_events_emit_nothing(self, handler):
        h, q = handler
        h()
        h(data="")
        h(current_tool_use={})
        assert self._drain(q) == []
