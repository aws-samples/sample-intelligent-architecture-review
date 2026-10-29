# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""In-process chat service for AI-driven rule curation.

User chats with this service to add or refine HA standards. The service
is a thin Bedrock Converse call wrapped around a structured-output
contract: the model returns plain assistant text plus zero-or-more
``<propose>{...}</propose>`` blocks describing new rules. We parse the
blocks, persist each as a ``status="proposed"`` standard, and return
both the text reply and the list of newly-created proposal IDs.

Why not another AgentCore agent?
- Same reasoning as validation: we don't need multi-turn tool use here.
  The model already knows the existing rules (we paste them into the
  system prompt) and writes proposals back through a structured tag.
- One round-trip per user message keeps latency to ~3-5 seconds.
- Falls back gracefully if the model ignores the contract — the chat
  reply still renders, just with zero proposals.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.services.validation_store import validation_store
from app.bedrock_models import SONNET, model_for
from app.services.bedrock_guardrail import wrap as _wrap_guardrail

logger = logging.getLogger(__name__)

# Sonnet 4.6, not Opus 4.7. Earlier we put this on Opus for "reasoning
# depth" but in practice the chat is a brainstorming surface where the
# user iterates over multiple turns — speed matters more than peak
# single-call quality, and Opus's latency was blowing through the
# ALB idle timeout (60s previously, 300s now, but Sonnet still
# returns ~5x faster which is more pleasant to use).
MODEL_ID = model_for("RULES_CHAT_MODEL_ID", SONNET)
MAX_HISTORY_MESSAGES = 20
# Restored to 5 (was temporarily capped at 3 to keep output small
# enough to fit CloudFront's 60s timeout). With async chat
# job processing, the LLM can take as long as it needs without
# breaking the request path, so 5 fits the hardcoded "AI Suggestion"
# prompt's "Up to 5 proposals per reply" wording and gives the user
# more breadth per turn.
MAX_PROPOSALS_PER_TURN = 5

# Allowed enum values for proposed rules. Anything outside these falls
# back to a sane default rather than being rejected — we don't want a
# model typo to wipe out the entire turn's proposals.
_VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_DEFAULT_SEVERITY = "medium"
_DEFAULT_CATEGORY = "general"

# hosting_scope vocabulary. Mirrors the Pydantic Literal in
# app/models/standard.py and the const tuple in frontend/src/lib/api.ts.
# The backend Literal IS the source of truth; the drift-guard test
# (test_hosting_scope_literal_matches_agent_vocab) ensures agents and
# this module stay in sync.
_VALID_HOSTING_SCOPES = {"on_prem", "cloud", "both"}
_DEFAULT_HOSTING_SCOPE = "both"

# Decision-tree rule vocabulary mirrored from app/models/standard.py. Curator
# proposals now carry these so approved rules land in the library
# fully tagged, like extracted ones do.
_VALID_LAYERS = {
    "datacenter",
    "data",
    "web",
    "application",
    "load_balancer",
    "general",
}
_DEFAULT_LAYER = "general"
_ALL_TIERS = [1, 2, 3, 4, 5]


def _make_bedrock_client():
    return _wrap_guardrail(boto3.client(
        "bedrock-runtime",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        config=Config(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        ),
    ))


SYSTEM_PROMPT = """You are an enterprise High Availability Standards Curator.

Your job is to help the user grow and refine their library of HA
(High Availability) validation standards. You see every existing
standard already in the library, and you can propose three kinds of
changes:

  - ADD a brand new rule
  - EDIT an existing rule (rename, retighten criteria, change severity, etc.)
  - DELETE an existing rule (duplicate, contradictory, obsolete)

# How to respond
Reply in plain prose. When you want to suggest a change, embed one
structured block per proposal in your reply using exactly this format:

<propose>
{
  "kind": "add" | "edit" | "delete",
  "target_id": "HA-D-XYZ",       // required for edit and delete
  "rationale": "1-2 sentences explaining why this change is worth making",
  // For add and edit, the FULL new field values:
  "name": "Short title for the rule",
  "description": "1-2 sentences describing what the rule requires",
  "severity": "critical | high | medium | low | info",
  "category": "failover | recovery | networking | observability | replication | security | storage | identity | general",
  "validation_criteria": "Specific, testable criteria an auditor would check against an SDD",
  // Required for add/edit so approved rules land fully tagged:
  "applies_to_tiers": [1, 2, 3, 4, 5],   // any subset of 1..5
  "layer": "datacenter | data | web | application | load_balancer | general"
}
</propose>

Rules of engagement:
- For "edit" proposals, return the FULL desired state of the rule, not
  a diff. The user will see a side-by-side current vs proposed view.
- For "delete" proposals, only kind, target_id, and rationale are
  required. The other fields are ignored.
- target_id must be the exact id of an existing standard from the
  library brief in the first user message. Don't invent ids.
- Use <propose> blocks ONLY when you genuinely want to suggest a
  change the user can act on. Conversational answers don't need them.
- Up to {max_proposals} proposals per reply. If you have more ideas,
  say so in prose and let the user ask for more.
- For "add" proposals: don't propose duplicates of existing rules.
- For "edit" / "delete": pick a clear winner — don't propose two
  competing edits to the same rule in one reply.
- Each proposal must be complete — no "TBD", no placeholders.
- Tier scope: if a rule clearly applies to a single tier (e.g. "Tier 1
  only" because the source matrix defines it that way), use that one
  tier; if the rule's wording applies broadly to all tiers, use [1,2,3,4,5].
- Layer: pick the single architectural layer the rule belongs to. Use
  "general" only when the rule isn't layer-specific (e.g. RTO/RPO targets,
  observability across the whole stack).

The text outside the <propose> blocks is what the user sees in the chat.
"""


_PROPOSE_RE = re.compile(r"<propose>\s*(.*?)\s*</propose>", re.DOTALL)


def _build_existing_standards_brief(standards: list[dict]) -> str:
    """Compact one-liner per existing standard for grounding context.

    The model only needs id/severity/category/name to reason about
    duplicates and gaps — full validation criteria would balloon the
    prompt by ~3× per row. If the library grows past MAX_BRIEF_RULES
    we truncate the tail and tell the model so it can ask the user
    to focus the question.
    """
    MAX_BRIEF_RULES = 250
    if not standards:
        return "(no standards exist yet)"
    truncated = standards[:MAX_BRIEF_RULES]
    lines: list[str] = []
    for s in truncated:
        sid = s.get("id", "?")
        name = s.get("name", "")
        sev = s.get("severity", "")
        cat = s.get("category", "")
        layer = s.get("layer") or "general"
        scope = s.get("hosting_scope") or "both"
        tiers = s.get("applies_to_tiers") or _ALL_TIERS
        if isinstance(tiers, list) and len(tiers) == len(_ALL_TIERS):
            tiers_repr = "T1-5"
        elif isinstance(tiers, list):
            tiers_repr = "T" + ",".join(str(t) for t in sorted(tiers))
        else:
            tiers_repr = "T?"
        lines.append(
            f"- [{sid}] [{sev}] [{cat}] [{layer}] [{tiers_repr}] [{scope}] {name}"
        )
    if len(standards) > MAX_BRIEF_RULES:
        lines.append(
            f"… and {len(standards) - MAX_BRIEF_RULES} more rules not shown. "
            "Ask the user to narrow the scope (by category or severity) "
            "if you need to see the full library."
        )
    return "\n".join(lines)


_VALID_KINDS = {"add", "edit", "delete"}


def _coerce_proposal(raw: Any) -> Optional[dict]:
    """Validate and normalize a single propose-block payload.

    Returns a dict with at minimum ``kind`` and ``rationale``; for
    add/edit also the rule fields; for edit/delete also ``target_id``.
    Drops the proposal if required fields are missing for the kind.
    Legacy proposals without an explicit kind are treated as "add"
    so older prompts still work.
    """
    if not isinstance(raw, dict):
        return None

    kind = str(raw.get("kind", "add")).strip().lower()
    if kind not in _VALID_KINDS:
        kind = "add"

    rationale = str(raw.get("rationale", "")).strip()
    target_id = str(raw.get("target_id", "")).strip() or None

    if kind in ("edit", "delete") and not target_id:
        # Can't act on an edit/delete without knowing the target.
        return None

    if kind == "delete":
        # Delete proposals only carry rationale + target.
        return {
            "kind": "delete",
            "target_id": target_id,
            "rationale": rationale,
        }

    # add or edit: full rule fields required.
    name = str(raw.get("name", "")).strip()
    description = str(raw.get("description", "")).strip()
    criteria = str(raw.get("validation_criteria", "")).strip()
    if not (name and description and criteria):
        return None

    severity = str(raw.get("severity", _DEFAULT_SEVERITY)).strip().lower()
    if severity not in _VALID_SEVERITIES:
        severity = _DEFAULT_SEVERITY
    category = str(raw.get("category", _DEFAULT_CATEGORY)).strip().lower() or _DEFAULT_CATEGORY

    # Tier + layer are carried on curator proposals so
    # approved ones land in the library with the same structured-rule
    # tags as extracted rules. Defaults are permissive: missing tiers
    # → all tiers; missing/unknown layer → "general". This ensures a
    # model that ignores the new fields still produces approvable
    # proposals rather than rejected ones.
    raw_tiers = raw.get("applies_to_tiers")
    if isinstance(raw_tiers, list) and raw_tiers:
        tiers = sorted({int(t) for t in raw_tiers if isinstance(t, (int, float)) and 1 <= int(t) <= 5})
        if not tiers:
            tiers = list(_ALL_TIERS)
    else:
        tiers = list(_ALL_TIERS)

    layer = str(raw.get("layer") or "").strip().lower() or _DEFAULT_LAYER
    if layer not in _VALID_LAYERS:
        layer = _DEFAULT_LAYER

    return {
        "kind": kind,
        "target_id": target_id,
        "rationale": rationale,
        "name": name,
        "description": description,
        "severity": severity,
        "category": category,
        "validation_criteria": criteria,
        "applies_to_tiers": tiers,
        "layer": layer,
    }


def _extract_proposals(text: str) -> list[dict]:
    """Pull all valid <propose> blocks out of the model output."""
    proposals: list[dict] = []
    for raw_block in _PROPOSE_RE.findall(text):
        try:
            payload = json.loads(raw_block)
        except json.JSONDecodeError:
            logger.warning("Could not JSON-parse <propose> block: %s", raw_block[:200])
            continue
        norm = _coerce_proposal(payload)
        if norm:
            proposals.append(norm)
        if len(proposals) >= MAX_PROPOSALS_PER_TURN:
            break
    return proposals


def _strip_propose_blocks(text: str) -> str:
    """Remove <propose>...</propose> blocks from the assistant-visible text."""
    cleaned = _PROPOSE_RE.sub("", text)
    # Collapse triple newlines that the removal can introduce.
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _persist_proposals(proposals: Iterable[dict], hosting_scope: str) -> list[dict]:
    """Save each proposal as a status="proposed" standard. Returns the saved rows."""
    saved: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    # Cache target lookups so we don't hammer DynamoDB if the agent
    # proposes several edits to the same rule.
    target_cache: dict[str, Optional[dict]] = {}

    def _target_exists(tid: str) -> bool:
        if tid not in target_cache:
            target_cache[tid] = validation_store.get_standard(tid)
        target = target_cache[tid]
        if not target:
            return False
        # Don't allow edits/deletes against rules that aren't currently
        # active — proposed and archived rows are out of scope here.
        return (target.get("status") or "active") == "active"

    for p in proposals:
        kind = p.get("kind", "add")

        if kind in ("edit", "delete"):
            target_id = p.get("target_id") or ""
            if not _target_exists(target_id):
                logger.warning(
                    f"Skipping {kind} proposal: target {target_id} not active or not found"
                )
                continue

        record: dict[str, Any] = {
            "id": f"HA-G-{uuid.uuid4().hex[:8].upper()}",
            "source": "generated",
            "status": "proposed",
            "proposal_kind": kind,
            "created_at": now,
            "updated_at": now,
            # Every saved rule needs a hosting_scope. Curate
            # picks one in the UI and threads it here so the proposal
            # lands in the same scope view as the conversation that
            # generated it.
            "hosting_scope": hosting_scope,
        }
        if p.get("target_id"):
            record["target_id"] = p["target_id"]
        if p.get("rationale"):
            record["rationale"] = p["rationale"]

        if kind == "delete":
            # Carry a friendly summary on the proposal row itself so
            # list views work without joining against the target.
            target = target_cache.get(p["target_id"]) or {}
            record["name"] = f"Delete: {target.get('name', p['target_id'])}"
            record["description"] = p.get("rationale") or "Proposed deletion"
            record["severity"] = target.get("severity", _DEFAULT_SEVERITY)
            record["category"] = target.get("category", _DEFAULT_CATEGORY)
            record["validation_criteria"] = "(no validation — this is a delete proposal)"
        else:
            # add or edit: full new field values supplied
            record["name"] = p["name"]
            record["description"] = p["description"]
            record["severity"] = p["severity"]
            record["category"] = p["category"]
            record["validation_criteria"] = p["validation_criteria"]
            record["applies_to_tiers"] = p["applies_to_tiers"]
            record["layer"] = p["layer"]

        validation_store.create_standard(record)
        saved.append(record)
    return saved


def _prepare_converse(
    messages: list[dict], hosting_scope: Optional[str]
) -> tuple[Any, list[dict], str, str]:
    """Build everything a Converse call needs from the raw chat turns.

    Shared by the synchronous ``chat()`` and the streaming
    ``chat_stream()`` so the two can never drift in how they scope the
    library brief, trim history, or shape the system prompt.

    Returns ``(bedrock_client, converse_messages, system_text, scope)``.
    Raises ``ValueError`` when there are no usable turns.
    """
    if not messages:
        raise ValueError("messages must contain at least one entry")

    # Normalize invalid or missing scope to universal so the service
    # does not assume an organization's deployment model.
    scope = (hosting_scope or "").strip().lower()
    if scope not in _VALID_HOSTING_SCOPES:
        scope = _DEFAULT_HOSTING_SCOPE

    existing = validation_store.list_standards(include_proposed=True)
    if scope == "both":
        scoped_existing = [s for s in existing if s.get("hosting_scope") == "both"]
    else:
        scoped_existing = [
            s
            for s in existing
            if s.get("hosting_scope") == scope or s.get("hosting_scope") == "both"
        ]
    brief = _build_existing_standards_brief(scoped_existing)

    # Trim conversation to the most recent N turns to keep prompts small.
    history = messages[-MAX_HISTORY_MESSAGES:]
    converse_messages = []
    for i, m in enumerate(history):
        role = m.get("role")
        content = str(m.get("content", "")).strip()
        if role not in ("user", "assistant") or not content:
            continue
        if i == 0 and role == "user":
            content = (
                f"Existing standards in our library:\n{brief}\n\n"
                f"User question:\n{content}"
            )
        converse_messages.append({"role": role, "content": [{"text": content}]})

    if not converse_messages:
        raise ValueError("no user/assistant turns to send")

    bedrock = _make_bedrock_client()
    system_text = SYSTEM_PROMPT.replace("{max_proposals}", str(MAX_PROPOSALS_PER_TURN))
    return bedrock, converse_messages, system_text, scope


def _finalize_turn(raw_text: str, scope: str) -> dict:
    """Turn the model's complete raw output into the persisted result.

    Shared tail of ``chat()`` and ``chat_stream()``: extract + persist
    proposals, strip the ``<propose>`` blocks from the visible reply, and
    synthesize a fallback reply when the model returned only proposals.
    """
    proposals_payload = _extract_proposals(raw_text)
    saved = _persist_proposals(proposals_payload, scope) if proposals_payload else []

    reply_text = _strip_propose_blocks(raw_text)
    if not reply_text:
        if saved:
            reply_text = (
                f"I added {len(saved)} draft standard"
                + ("" if len(saved) == 1 else "s")
                + " for your review."
            )
        else:
            reply_text = "(no reply)"
    return {"reply": reply_text, "proposals": saved}


# ---------------------------------------------------------------------------
# Streaming (SSE) support
# ---------------------------------------------------------------------------

# Extended-thinking budget for the streaming path. When > 0 we enable
# Bedrock's reasoning_config so the model emits reasoningContent deltas the
# UI renders as a live "thinking" trace. Anthropic requires temperature=1
# and maxTokens > budget when reasoning is on, so we can't reuse the
# synchronous call's temperature=0.2. Set to 0 to stream answer tokens
# only (cheaper, lower latency).
_REASONING_BUDGET = int(os.getenv("RULES_CHAT_REASONING_BUDGET", "1024"))
_STREAM_MAX_TOKENS = int(os.getenv("RULES_CHAT_STREAM_MAX_TOKENS", "4000"))


class _ProposeStreamFilter:
    """Strip ``<propose>...</propose>`` blocks out of a token stream.

    The model interleaves user-facing prose with machine-readable
    ``<propose>`` blocks. When streaming we must show the prose live but
    never leak raw proposal JSON into the chat bubble. This is the classic
    "strip a tag across chunk boundaries" problem: a tag can split across
    two deltas, so we hold back a short tail that might be the start of a
    tag until we've seen enough to decide.

    ``feed(chunk)`` returns the text that is safe to emit now.
    ``flush()`` returns any trailing held-back text once the stream ends.
    """

    _OPEN = "<propose>"
    _CLOSE = "</propose>"
    # Longest prefix we might need to hold back = the longer tag length - 1.
    _MAX_HOLD = max(len(_OPEN), len(_CLOSE)) - 1

    def __init__(self) -> None:
        self._buf = ""
        self._in_block = False
        # Running count of <propose> blocks that have STARTED. chat_stream
        # reads this to emit a "drafting proposal N" progress signal during
        # the otherwise-silent stretch where the model is emitting proposal
        # JSON (which we suppress from the visible reply).
        self.blocks_started = 0

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while True:
            if not self._in_block:
                idx = self._buf.find(self._OPEN)
                if idx != -1:
                    out.append(self._buf[:idx])
                    self._buf = self._buf[idx + len(self._OPEN):]
                    self._in_block = True
                    self.blocks_started += 1
                    continue
                # No full open tag. Emit everything except a trailing
                # fragment that could be the beginning of "<propose>".
                safe = self._emittable_prefix_len(self._buf)
                out.append(self._buf[:safe])
                self._buf = self._buf[safe:]
                break
            else:
                idx = self._buf.find(self._CLOSE)
                if idx != -1:
                    # Discard the block body + closing tag entirely.
                    self._buf = self._buf[idx + len(self._CLOSE):]
                    self._in_block = False
                    continue
                # Still inside a block; discard all but a possible partial
                # closing tag so we can detect it once more arrives.
                keep = min(len(self._buf), self._MAX_HOLD)
                self._buf = self._buf[len(self._buf) - keep:]
                break
        return "".join(out)

    def _emittable_prefix_len(self, s: str) -> int:
        """Length of ``s`` safe to emit without risking a split open tag.

        Hold back the longest suffix of ``s`` that is a proper prefix of
        ``<propose>`` (e.g. a trailing ``"<prop"``), since the rest of the
        tag may arrive in the next chunk.
        """
        max_check = min(len(s), len(self._OPEN) - 1)
        for hold in range(max_check, 0, -1):
            if self._OPEN.startswith(s[-hold:]):
                return len(s) - hold
        return len(s)

    def flush(self) -> str:
        """Emit any remaining safe text. Anything still inside an unclosed
        block is dropped (a truncated proposal isn't user-facing prose)."""
        if self._in_block:
            self._buf = ""
            return ""
        out = self._buf
        self._buf = ""
        return out


def chat_stream(messages: list[dict], hosting_scope: Optional[str] = None):
    """Streaming variant of :func:`chat`.

    A **blocking generator** (the caller runs it in an executor thread and
    forwards events onto an asyncio queue — see the ``/chat/stream`` route).
    Yields ``(kind, payload)`` tuples:

      - ``("thinking", str)`` — a reasoningContent delta (extended thinking)
      - ``("delta", str)``    — user-facing reply text, ``<propose>`` blocks
                                already stripped
      - ``("proposal_progress", int)`` — the Nth ``<propose>`` block just
                                started; the stream goes silent while its
                                JSON is drafted, so the UI shows progress
      - ``("proposals", list)`` — the saved proposal rows, emitted once
      - ``("reply", str)``    — the FINAL canonical reply (with the same
                                fallback text as ``chat()`` when the model
                                emitted only proposals); emitted once, last.
                                Used to persist the turn so it survives a
                                mid-stream refresh.
      - ``("error", str)``    — Bedrock or setup failure; terminal

    The full raw model output (proposals included) is accumulated so
    proposal extraction/persistence matches the synchronous path exactly.
    """
    try:
        bedrock, converse_messages, system_text, scope = _prepare_converse(
            messages, hosting_scope
        )
    except ValueError as e:
        yield ("error", str(e))
        return

    inference: dict[str, Any] = {"maxTokens": _STREAM_MAX_TOKENS}
    extra_fields: Optional[dict] = None
    if _REASONING_BUDGET > 0:
        # Reasoning on: Anthropic forces temperature=1 and maxTokens must
        # exceed the thinking budget.
        inference["temperature"] = 1.0
        inference["maxTokens"] = max(_STREAM_MAX_TOKENS, _REASONING_BUDGET + 1000)
        extra_fields = {
            "reasoning_config": {"type": "enabled", "budget_tokens": _REASONING_BUDGET}
        }
    else:
        inference["temperature"] = 0.2

    kwargs: dict[str, Any] = {
        "modelId": MODEL_ID,
        "system": [{"text": system_text}],
        "messages": converse_messages,
        "inferenceConfig": inference,
    }
    if extra_fields:
        kwargs["additionalModelRequestFields"] = extra_fields

    prose_filter = _ProposeStreamFilter()
    raw_parts: list[str] = []
    last_blocks = 0
    try:
        response = bedrock.converse_stream(**kwargs)
        for ev in response["stream"]:
            block = ev.get("contentBlockDelta")
            if not block:
                continue
            delta = block.get("delta", {})
            reasoning = delta.get("reasoningContent")
            if reasoning:
                rtext = reasoning.get("text", "")
                if rtext:
                    yield ("thinking", rtext)
                continue
            text = delta.get("text", "")
            if text:
                raw_parts.append(text)
                visible = prose_filter.feed(text)
                if visible:
                    yield ("delta", visible)
                # The model just began a <propose> block: its JSON is
                # suppressed, so without this the stream goes silent while
                # each proposal is drafted. Surface a progress signal so the
                # UI can show "drafting proposal N".
                if prose_filter.blocks_started > last_blocks:
                    last_blocks = prose_filter.blocks_started
                    yield ("proposal_progress", last_blocks)
    except ClientError as e:
        logger.warning(f"Bedrock chat stream failed: {e}")
        yield ("error", "I couldn't reach the AI service. Please try again in a moment.")
        return
    except Exception as e:  # noqa: BLE001 — surfaced to the client as an error frame
        logger.exception("rules curator chat stream failed")
        yield ("error", str(e))
        return

    tail = prose_filter.flush()
    if tail:
        yield ("delta", tail)

    # Persist proposals from the COMPLETE raw output, identical to chat().
    result = _finalize_turn("".join(raw_parts), scope)
    yield ("proposals", result["proposals"])
    # Canonical reply LAST, so the route can persist the turn (reply +
    # proposals) for refresh-durable resume. This matches chat()'s reply,
    # including its "I added N draft standards" fallback when the model
    # returned only proposals with no prose.
    yield ("reply", result["reply"])


def chat(messages: list[dict], hosting_scope: Optional[str] = None) -> dict:
    """Single chat turn. Returns ``{"reply": str, "proposals": [<saved standard>]}``.

    ``messages`` is the conversation so far in OpenAI/Bedrock-style
    ``[{"role": "user"|"assistant", "content": "..."}]`` order, ending
    with the latest user message.

    ``hosting_scope`` restricts the library brief to rules
    whose scope matches the chosen view (or "both"), and tags every
    saved proposal with that scope so it appears in the same Standards
    page segment that produced it. Defaults to "both" when missing.
    """
    bedrock, converse_messages, system_text, scope = _prepare_converse(
        messages, hosting_scope
    )

    try:
        response = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": system_text}],
            messages=converse_messages,
            # Sonnet 4.6 still supports temperature; keep it low so
            # repeated calls with the same context produce stable
            # rule drafts.
            inferenceConfig={"maxTokens": 4000, "temperature": 0.2},
        )
        content = response["output"]["message"]["content"]
        raw_text = "".join(block.get("text", "") for block in content)
    except ClientError as e:
        logger.warning(f"Bedrock chat call failed: {e}")
        return {
            "reply": (
                "I couldn't reach the AI service. Please try again in a moment."
            ),
            "proposals": [],
        }

    return _finalize_turn(raw_text, scope)
