# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Streaming curator chat — the SSE path added for live "thinking" + reply.

Two layers of coverage, both without a live Bedrock:

1. ``_ProposeStreamFilter`` — pure logic that strips ``<propose>`` blocks
   out of a token stream. This is the risky bit: a tag can split across
   chunk boundaries, and a bug here would leak raw proposal JSON into the
   chat bubble. Locked against regressions across every split pattern.

2. ``chat_stream`` — driven with a fake ``converse_stream`` so we assert
   the (kind, payload) event sequence: reasoning → thinking, text →
   delta (blocks stripped), and that proposals are PERSISTED and emitted
   in the terminal ``proposals`` event (the whole point — no
   read-after-write race on the client).
"""

from __future__ import annotations

import pytest

from app.services import rules_curator_chat as rc
from app.services.validation_store import validation_store


# ---------------------------------------------------------------------------
# _ProposeStreamFilter
# ---------------------------------------------------------------------------


def _run(chunks: list[str]) -> str:
    f = rc._ProposeStreamFilter()
    out = "".join(f.feed(c) for c in chunks)
    return out + f.flush()


FULL = 'Here is a rule. <propose>{"kind":"add"}</propose> Done.'
EXPECTED = "Here is a rule.  Done."


def test_filter_whole_string():
    assert _run([FULL]) == EXPECTED


def test_filter_one_char_at_a_time():
    # Worst case: every tag character arrives in its own chunk.
    assert _run(list(FULL)) == EXPECTED


def test_filter_tag_split_across_boundary():
    assert _run(["prefix <prop", "ose>{...}</prop", "ose> suffix"]) == "prefix  suffix"


def test_filter_passthrough_no_block():
    assert _run(["hello ", "world"]) == "hello world"


def test_filter_partial_open_tag_never_completed_is_emitted():
    # A trailing "<pro" that never becomes "<propose>" must still surface
    # at flush — it was real prose, not a tag.
    assert _run(["answer <pro"]) == "answer <pro"


def test_filter_multiple_blocks():
    t = "a<propose>X</propose>b<propose>Y</propose>c"
    assert _run([t]) == "abc"
    assert _run(list(t)) == "abc"


def test_filter_unclosed_block_body_dropped():
    # A block opened but never closed (truncated stream) drops its body.
    assert _run(["keep <propose>{unterm"]) == "keep "


def test_filter_counts_blocks_started():
    f = rc._ProposeStreamFilter()
    f.feed("a<propose>X</propose>b<propose>Y</propose>c")
    assert f.blocks_started == 2
    # Split across chunks still counts once per block.
    g = rc._ProposeStreamFilter()
    g.feed("a<prop")
    g.feed("ose>X</propose>")
    assert g.blocks_started == 1


# ---------------------------------------------------------------------------
# chat_stream — with a fake Bedrock converse_stream
# ---------------------------------------------------------------------------


class _FakeBedrock:
    """Minimal stand-in whose converse_stream yields a scripted event list."""

    def __init__(self, events):
        self._events = events

    def converse_stream(self, **kwargs):
        return {"stream": iter(self._events)}


def _text_ev(text: str) -> dict:
    return {"contentBlockDelta": {"delta": {"text": text}}}


def _reasoning_ev(text: str) -> dict:
    return {"contentBlockDelta": {"delta": {"reasoningContent": {"text": text}}}}


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch):
    # Keep the in-memory store deterministic and offline.
    monkeypatch.setattr(validation_store, "_available", False)
    validation_store._standards = {}
    yield
    validation_store._standards = {}


def _collect(monkeypatch, events, reasoning_budget=0):
    monkeypatch.setattr(rc, "_make_bedrock_client", lambda: _FakeBedrock(events))
    monkeypatch.setattr(rc, "_REASONING_BUDGET", reasoning_budget)
    msgs = [{"role": "user", "content": "Add a rule about RTO for Tier-1."}]
    return list(rc.chat_stream(msgs, hosting_scope="both"))


def test_chat_stream_emits_thinking_then_delta(monkeypatch):
    events = [
        _reasoning_ev("Let me consider Tier-1 RTO."),
        _text_ev("Here is a suggestion. "),
        _text_ev("It targets RTO."),
    ]
    out = _collect(monkeypatch, events, reasoning_budget=1024)

    kinds = [k for k, _ in out]
    assert kinds[0] == "thinking"
    assert "thinking" in kinds and "delta" in kinds
    # Terminal event is the canonical reply; proposals come just before it.
    assert out[-1][0] == "reply"
    assert out[-2][0] == "proposals"

    thinking = "".join(p for k, p in out if k == "thinking")
    reply = "".join(p for k, p in out if k == "delta")
    assert "Tier-1 RTO" in thinking
    assert "Here is a suggestion." in reply
    assert "RTO" in reply
    # The reply event mirrors the streamed delta text.
    reply_payload = [p for k, p in out if k == "reply"][0]
    assert "Here is a suggestion." in reply_payload


def test_chat_stream_strips_propose_and_persists(monkeypatch):
    # A complete propose block, split so the tag straddles a chunk.
    propose = (
        '<propose>{"kind":"add","name":"RTO Tier-1",'
        '"description":"Tier-1 systems must define an RTO.",'
        '"validation_criteria":"An RTO value is documented for Tier-1.",'
        '"severity":"high","category":"availability"}</propose>'
    )
    events = [
        _text_ev("Sure — I drafted one. "),
        _text_ev(propose[:20]),
        _text_ev(propose[20:]),
        _text_ev(" Review it above."),
    ]
    out = _collect(monkeypatch, events)

    reply = "".join(p for k, p in out if k == "delta")
    # The propose JSON must NEVER appear in the user-visible reply.
    assert "<propose>" not in reply
    assert "validation_criteria" not in reply
    assert "Sure — I drafted one." in reply
    assert "Review it above." in reply

    # The proposals event carries the persisted row, and it's in the store.
    proposals_events = [p for k, p in out if k == "proposals"]
    assert len(proposals_events) == 1
    saved = proposals_events[0]
    assert len(saved) == 1
    assert saved[0]["name"] == "RTO Tier-1"
    assert saved[0]["status"] == "proposed"
    assert saved[0]["hosting_scope"] == "both"
    # Actually persisted (same store a manual refresh would read).
    assert saved[0]["id"] in validation_store._standards


def test_chat_stream_emits_proposal_progress(monkeypatch):
    # Two proposals → two progress signals, numbered 1 then 2.
    p1 = (
        '<propose>{"kind":"add","name":"Rule A","description":"desc A here.",'
        '"validation_criteria":"criteria A."}</propose>'
    )
    p2 = (
        '<propose>{"kind":"add","name":"Rule B","description":"desc B here.",'
        '"validation_criteria":"criteria B."}</propose>'
    )
    events = [
        _text_ev("Here are two: "),
        _text_ev(p1),
        _text_ev(" and "),
        _text_ev(p2),
        _text_ev(" done."),
    ]
    out = _collect(monkeypatch, events)

    progress = [p for k, p in out if k == "proposal_progress"]
    assert progress == [1, 2]
    # Both persisted.
    saved = [p for k, p in out if k == "proposals"][0]
    assert {r["name"] for r in saved} == {"Rule A", "Rule B"}


def test_chat_stream_reply_fallback_when_only_proposals(monkeypatch):
    # Model emits ONLY a propose block, no prose. The reply event must
    # carry the synthesized "I added N draft standard(s)" fallback.
    propose = (
        '<propose>{"kind":"add","name":"Only Rule","description":"desc here.",'
        '"validation_criteria":"criteria here."}</propose>'
    )
    out = _collect(monkeypatch, [_text_ev(propose)])
    reply_payload = [p for k, p in out if k == "reply"][0]
    assert "1 draft standard" in reply_payload
    # No visible delta leaked the JSON.
    assert "".join(p for k, p in out if k == "delta").strip() == ""


def test_chat_stream_error_frame_on_client_failure(monkeypatch):
    class _Boom:
        def converse_stream(self, **kwargs):
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "Throttling"}}, "ConverseStream")

    monkeypatch.setattr(rc, "_make_bedrock_client", lambda: _Boom())
    monkeypatch.setattr(rc, "_REASONING_BUDGET", 0)
    out = list(rc.chat_stream([{"role": "user", "content": "hi"}], hosting_scope="both"))
    assert out and out[-1][0] == "error"
