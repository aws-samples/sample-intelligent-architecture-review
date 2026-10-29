# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Standards routes - Dynamic HA validation standards from DynamoDB.

Standards come from two sources:
1. Document-based: Extracted from uploaded standards documents (PDFs) via Strands agent
2. User-defined: Created/edited/deleted by users through the frontend UI, stored in DynamoDB
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.rbac import Role, require_role
from app.models.standard import (
    CreateStandardRequest,
    StandardSource,
    UpdateStandardRequest,
)
from app.services.audit_store import audit_store
from app.services.rules_curator_chat import chat as rules_chat
from app.services.rules_curator_chat import chat_stream as rules_chat_stream
from app.services.standards_curator import curate_standards
from app.services.validation_store import validation_store


def _audit(claims, action, entity_type, entity_id, detail=None):
    """Best-effort audit record — never breaks the request it audits."""
    try:
        audit_store.record_from_claims(
            claims, action=action, entity_type=entity_type,
            entity_id=entity_id, detail=detail,
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("audit record failed (non-fatal)")


class CurateStandardsRequest(BaseModel):
    document_id: str = Field(description="Document to rank standards against")
    top_n: int = Field(default=20, ge=1, le=200, description="How many existing standards to return")
    user_prompt: Optional[str] = Field(
        default=None,
        description="Free-text guidance from the user about what they care about for this validation.",
    )
    suggest_new: bool = Field(
        default=True,
        description="When true, the model may also propose brand-new rules (not yet saved).",
    )
    max_new: int = Field(
        default=5,
        ge=0,
        le=20,
        description="Cap on how many new-rule suggestions to return.",
    )


class NewSuggestion(BaseModel):
    name: str
    description: str
    severity: str
    category: str
    validation_criteria: str
    rationale: str = ""


class CurateStandardsResponse(BaseModel):
    existing_standard_ids: list[str]
    new_suggestions: list[NewSuggestion]
    total_available: int
    top_n: int


class ChatTurn(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatTurn] = Field(
        default_factory=list,
        description="Conversation so far, ending with the latest user message",
    )
    # The Curate page picks a hosting scope and the chat agent's
    # library brief is filtered + proposals tagged accordingly. Optional
    # for backward compat with tests/clients that don't set it; the
    # service defaults to universal when missing.
    hosting_scope: Optional[Literal["on_prem", "cloud", "both"]] = Field(
        default=None,
        description=(
            "Curate scope. Filters the library brief sent to the agent "
            "and tags every persisted proposal so it lands in the same "
            "scope on the Standards page."
        ),
    )


class ChatResponse(BaseModel):
    reply: str
    proposals: list[dict] = Field(
        default_factory=list,
        description="Newly created proposed standards (status='proposed')",
    )


# Async chat job. POST returns 202 with {job_id, status},
# GET polls /chat/jobs/{job_id} for the final reply + proposals once
# the background Bedrock call completes.
class ChatJobAcceptedResponse(BaseModel):
    job_id: str
    status: str = "pending"


class ChatJobResponse(BaseModel):
    id: str
    status: str  # pending | running | completed | failed
    hosting_scope: str
    created_at: str
    updated_at: str
    reply: Optional[str] = None
    proposals: list[dict] = Field(default_factory=list)
    error: Optional[str] = None


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def list_standards(
    include_proposed: bool = False,
    status: str | None = None,
    hosting_scope: str | None = None,
    source: str | None = None,
):
    """List standards.

    By default returns only active rules (validation runs use the same
    list, so proposals must not leak in). The proposals panel calls
    this with ``status=proposed`` to fetch only AI suggestions awaiting
    review, or ``include_proposed=true`` to get the full set.

    ``hosting_scope`` (optional) restricts the result to rules tagged
    with that scope. The Curate page's proposal panel uses this so
    each scope tab only shows proposals generated in that tab — earlier
    they all bled together in one shared list.

    ``source`` (optional) restricts to a rule origin: ``generated``
    (curator-chat proposals) or ``document`` (extracted from a standards
    doc). The Curate proposals panel passes ``source=generated`` so
    document-extraction batches — which now also land as ``proposed`` and
    are reviewed on the document-detail page — don't bleed into it.
    """
    if status not in (None, "active", "proposed"):
        raise HTTPException(status_code=400, detail=f"Invalid status filter: {status}")
    if hosting_scope not in (None, "on_prem", "cloud", "both"):
        raise HTTPException(
            status_code=400, detail=f"Invalid hosting_scope filter: {hosting_scope}"
        )
    if source not in (None, "generated", "document", "user"):
        raise HTTPException(status_code=400, detail=f"Invalid source filter: {source}")

    if status == "proposed":
        # Filter for proposals only — DON'T pass include_proposed=True or
        # the store short-circuits and returns every row regardless.
        standards = validation_store.list_standards(status="proposed")
    elif include_proposed:
        standards = validation_store.list_standards(status=None, include_proposed=True)
    else:
        standards = validation_store.list_standards()  # active only by default

    if hosting_scope is not None:
        standards = [
            s for s in standards if (s.get("hosting_scope") or "both") == hosting_scope
        ]
    if source is not None:
        standards = [s for s in standards if s.get("source") == source]

    return {"standards": standards, "total": len(standards)}


@router.get("/extraction-status")
async def get_extraction_status(
    document_id: str | None = None,
    since_seq: int = 0,
):
    """Get extraction job status for real-time tracking.

    Modes:
    - default → snapshot of every active/recent job, used by the
      Documents/Standards page banner.
    - ?document_id=doc-XXX&since_seq=N → tail of the live log for one
      job, used by the doc-detail page log panel. Returns log entries
      with seq > since_seq so the client can poll incrementally.
    """
    from app.services.extraction_log import (
        get_active_jobs,
        get_completed_jobs,
        get_failed_jobs,
        get_job,
        get_log_since,
    )

    current_standards = validation_store.list_standards()
    doc_standards_count = sum(1 for s in current_standards if s.get("source") == "document")

    if document_id:
        job = get_job(document_id)
        return {
            "document_id": document_id,
            "job": job,
            "log": get_log_since(document_id, since_seq=since_seq),
            "total_extracted": doc_standards_count,
        }

    # Use the extraction-log registry for the snapshot. The older
    # _extraction_jobs dict is left untouched for backward compat with the manual
    # /extract endpoint's response shape, but the banner reads from
    # extraction_log which sees BOTH manual + auto-trigger paths.
    active_jobs = get_active_jobs()
    completed_jobs = get_completed_jobs(limit=5)
    failed_jobs = get_failed_jobs(limit=3)

    return {
        "is_extracting": len(active_jobs) > 0,
        "active_jobs": active_jobs,
        "completed_jobs": completed_jobs,
        "failed_jobs": failed_jobs,
        "total_extracted": doc_standards_count,
        "total_standards": len(current_standards),
    }


@router.post("/curate", response_model=CurateStandardsResponse)
async def curate_standards_for_document(
    request: CurateStandardsRequest,
    # Drives a Bedrock call and selects the rules a run is judged
    # against — not something an unprivileged caller should trigger.
    # Both writing roles qualify: a validator reaches it from the Run
    # Validation dialog, and a curator owns the rule library it selects from.
    # It writes nothing, so neither role gains a capability it lacks
    # elsewhere; gating on validator alone locked curators out of AI
    # curation of their own library.
    claims: dict = Depends(require_role(Role.VALIDATOR, Role.CURATOR)),
):
    """Pick the top-N most applicable standards for a given document.

    Single Bedrock call (Haiku 4.5). Used by the AI-curated mode of the
    Run Validation dialog so a user can say "validate against the 20 most
    relevant rules" without scrolling the full list.
    """
    document = validation_store.get_document(request.document_id)
    if not document:
        raise HTTPException(
            status_code=404, detail=f"Document {request.document_id} not found"
        )

    doc_status = document.status.value if hasattr(document.status, "value") else str(document.status)
    if doc_status not in ("parsed", "analyzed"):
        raise HTTPException(
            status_code=400,
            detail=f"Document must be parsed before curation. Current status: {doc_status}",
        )

    standards = validation_store.list_standards()
    if not standards:
        raise HTTPException(
            status_code=400,
            detail="No standards configured to curate from.",
        )

    # Run the (sync) Bedrock call in a worker thread so we don't block the
    # event loop. Curation is fast (~3-5s) so a synchronous response is fine.
    result = await asyncio.to_thread(
        curate_standards,
        document,
        standards,
        request.top_n,
        user_prompt=request.user_prompt,
        max_new=request.max_new if request.suggest_new else 0,
    )
    return CurateStandardsResponse(
        existing_standard_ids=result.get("existing_standard_ids", []),
        new_suggestions=[NewSuggestion(**s) for s in result.get("new_suggestions", [])],
        total_available=len(standards),
        top_n=request.top_n,
    )


@router.post(
    "/chat", response_model=ChatJobAcceptedResponse, status_code=202,
    dependencies=[Depends(require_role(Role.CURATOR))],
)
async def chat_with_rules_curator(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
):
    """Kick off an async chat turn with the AI rule curator.

    A synchronous form would hold the HTTP request open while Bedrock
    generates the reply (often >60s for a multi-propose turn), which
    CloudFront 504s. Instead we create a job record in DynamoDB, return
    202 immediately, and run the Bedrock call in a FastAPI
    BackgroundTask. The frontend polls GET /chat/jobs/{job_id} until
    status terminal.
    """
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")
    if request.messages[-1].role != "user":
        raise HTTPException(
            status_code=400,
            detail="The last message in the conversation must be from the user.",
        )

    from app.services.chat_jobs_store import chat_jobs_store

    serialized = [m.model_dump() for m in request.messages]
    scope = request.hosting_scope or "both"
    job_id = chat_jobs_store.create(serialized, hosting_scope=scope)

    background_tasks.add_task(_run_chat_job, job_id, serialized, scope)
    return ChatJobAcceptedResponse(job_id=job_id, status="pending")


async def _run_chat_job(
    job_id: str, messages: list[dict], hosting_scope: str
) -> None:
    """Background worker: run rules_chat in a thread, write outcome
    to the job record. Failures are caught and recorded so the
    frontend's poller sees status=failed instead of hanging."""
    from app.services.chat_jobs_store import chat_jobs_store

    chat_jobs_store.mark_running(job_id)
    try:
        result = await asyncio.to_thread(
            rules_chat, messages, hosting_scope=hosting_scope
        )
        chat_jobs_store.complete(
            job_id,
            reply=result.get("reply", ""),
            proposals=result.get("proposals", []),
        )
    except Exception as e:
        logger.exception(f"rules curator chat job {job_id} failed")
        chat_jobs_store.fail(job_id, str(e))


@router.get("/chat/jobs/{job_id}", response_model=ChatJobResponse)
async def get_chat_job(job_id: str):
    """Poll a chat job. Frontend hits this every ~1.5s until status
    is `completed` or `failed`. Returns the saved proposals + reply
    on completion so the Curate page can render them."""
    from app.services.chat_jobs_store import chat_jobs_store

    job = chat_jobs_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Chat job {job_id} not found")
    return ChatJobResponse(
        id=job["id"],
        status=job["status"],
        hosting_scope=job.get("hosting_scope", "both"),
        created_at=job.get("created_at", ""),
        updated_at=job.get("updated_at", ""),
        reply=job.get("reply"),
        proposals=job.get("proposals", []),
        error=job.get("error"),
    )


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Events frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/chat/stream", dependencies=[Depends(require_role(Role.CURATOR))])
async def chat_with_rules_curator_stream(request: ChatRequest):
    """Streaming (SSE) variant of the curator chat.

    Where POST /chat returns a job_id the client must poll, this holds the
    connection open and streams the turn live:

      - ``event: job``      — ``{job_id}``, sent FIRST. The client remembers
                              it so a mid-stream refresh/navigate can resume
                              via GET /chat/jobs/{job_id} and recover the
                              reply (see below).
      - ``event: thinking`` — extended-thinking (reasoning) deltas
      - ``event: delta``    — user-facing reply text (``<propose>`` blocks
                              already stripped by the service)
      - ``event: proposal_progress`` — ``{n}``; the Nth proposal is being
                              drafted (its JSON is suppressed, so the token
                              stream is otherwise silent during this stretch)
      - ``event: proposals``— the saved proposal rows, once
      - ``event: error``    — Bedrock/setup failure; terminal
      - ``event: done``     — stream complete

    Durability: a chat_jobs_store record is created up front and the reply +
    proposals are persisted from the PUMP THREAD (which keeps running even
    if the client disconnects mid-stream). So a refresh/navigate before the
    stream finishes doesn't lose the reply — the frontend's useCuratorJobs
    hook resumes the remembered job and appends it. Proposals are also
    persisted inside chat_stream (identical to the sync path) before the
    ``proposals`` frame, so the panel renders with no read-after-write race.

    Frames set ``Cache-Control: no-transform`` so CloudFront (compress=true
    on /api/*) doesn't buffer the response and defeat token streaming.
    """
    from fastapi.responses import StreamingResponse
    from app.services.chat_jobs_store import chat_jobs_store

    if not request.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")
    if request.messages[-1].role != "user":
        raise HTTPException(
            status_code=400,
            detail="The last message in the conversation must be from the user.",
        )

    serialized = [m.model_dump() for m in request.messages]
    scope = request.hosting_scope or "both"

    # Create the durable job record up front. Best-effort — if DDB is
    # unavailable the live stream still works; only refresh-resume is lost.
    job_id = None
    try:
        job_id = chat_jobs_store.create(serialized, hosting_scope=scope)
        chat_jobs_store.mark_running(job_id)
    except Exception:  # noqa: BLE001
        logger.warning("chat stream: could not create durable job record", exc_info=True)

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _pump():
            """Drive the blocking chat_stream generator off the event loop,
            forwarding its (kind, payload) tuples onto the asyncio queue.

            Also accumulates the final reply + proposals and persists them to
            the job store. This runs in the executor thread, so it completes
            (and persists) even if the client has disconnected and the
            async generator below is no longer being consumed."""
            final_reply = ""
            final_proposals: list = []
            errored = False
            try:
                for kind, payload in rules_chat_stream(serialized, hosting_scope=scope):
                    if kind == "proposals":
                        final_proposals = payload
                    elif kind == "reply":
                        final_reply = payload
                    elif kind == "error":
                        errored = True
                    loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))
            except Exception as exc:  # noqa: BLE001 — surfaced as an SSE error frame
                logger.exception("curator chat stream pump failed")
                errored = True
                final_reply = str(exc)
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
            finally:
                # Persist terminal state so a resumed poll recovers the turn.
                if job_id:
                    try:
                        if errored:
                            chat_jobs_store.fail(job_id, final_reply or "stream failed")
                        else:
                            chat_jobs_store.complete(
                                job_id, reply=final_reply, proposals=final_proposals
                            )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            f"chat stream: could not persist job {job_id}",
                            exc_info=True,
                        )
                loop.call_soon_threadsafe(queue.put_nowait, ("eos", None))

        loop.run_in_executor(None, _pump)

        # job frame first so the client can remember it before any tokens.
        if job_id:
            yield _sse("job", {"job_id": job_id})

        while True:
            kind, payload = await queue.get()
            if kind == "eos":
                break
            elif kind == "thinking":
                yield _sse("thinking", {"text": payload})
            elif kind == "delta":
                yield _sse("delta", {"text": payload})
            elif kind == "proposal_progress":
                yield _sse("proposal_progress", {"n": payload})
            elif kind == "proposals":
                yield _sse("proposals", {"proposals": payload})
            elif kind == "reply":
                # Consumed for persistence in the pump; the client already
                # has the reply from the delta stream, so no frame needed.
                pass
            elif kind == "error":
                yield _sse("error", {"detail": payload})
                # Keep draining until eos so the pump thread finishes cleanly.
        yield _sse("done", {})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Review gate — extraction batches. Rules extracted from a standards document
# land status="proposed" tagged with an extraction_batch_id (see
# agents/extraction/run.py). These endpoints let the doc-detail review panel
# list a document's batches, read a batch's proposed rules, and bulk
# approve/reject. Defined BEFORE GET /{standard_id} so the literal
# "/extraction-batches" path isn't swallowed by the {standard_id} matcher.
# ---------------------------------------------------------------------------


import re as _re


def _extraction_in_flight_for(rule: dict) -> bool:
    """True when the extraction run that produced this rule is still going.

    Rules land status="proposed" per-unit DURING extraction, but the
    cross-reference wiring + semantic dedup (synthesis) run only AFTER all
    units finish, over the whole proposed batch. Approving/rejecting a rule
    before then pulls it out of that set — orphaning parent links and
    skipping dedup. So we refuse mutations while the owning job is in-flight.

    source_document is "<filename> (<document_id>)"; extract the id and check
    whether a run is active. Two sources, because the live-log job lives in ONE
    task's memory: a request that lands on a different ECS task sees no job and
    would fail open, letting a curator approve mid-run. The document row carries a
    persisted `extraction_state` for exactly that case.

    Fail OPEN (allow) only when neither source resolves, so an untracked or
    crashed job never permanently locks a rule.
    """
    src = rule.get("source_document") or ""
    m = _re.search(r"\(([^()]+)\)\s*$", src)
    doc_id = m.group(1).strip() if m else None
    if not doc_id:
        return False

    # Delegate the two-source (same-task live log + cross-task row stamp) check
    # to extraction_log.is_in_flight, so the approve/reject/set-scope gate and
    # the extraction start-guard can never diverge on what "in flight" means.
    try:
        from app.services.extraction_log import is_in_flight
        return is_in_flight(doc_id)
    except Exception:
        return False


def _guard_not_in_flight(rule: dict) -> None:
    """Raise 409 if the rule's extraction run hasn't finalized (dedup +
    cross-reference wiring). Keeps the review gate's accuracy guarantee."""
    if _extraction_in_flight_for(rule):
        raise HTTPException(
            status_code=409,
            detail=(
                "This document is still extracting. Rules can't be approved or "
                "rejected until extraction finishes and cross-references + "
                "deduplication are finalized."
            ),
        )


def _proposed_document_rules() -> list[dict]:
    """All proposed rules that came from document extraction (source=document).

    Excludes curator-chat proposals (source=generated) so the two review
    surfaces — the Curate proposals panel and the extraction review batch —
    never show each other's rows.
    """
    rows = validation_store.list_standards(status="proposed")
    return [r for r in rows if (r.get("source") == "document")]


def _summarize_batch(batch_id: str, rows: list[dict]) -> dict:
    """Build a batch summary from its proposed rows.

    ``in_flight`` is true while the run that produced this batch is still
    extracting or finalizing (dedup + cross-reference wiring run only after all
    units finish). Until then ``total`` is the PRE-dedup, per-unit row count — a
    document that restates the same rule across pages transiently inflates it,
    then it collapses to the final de-duplicated count. Callers should treat an
    in-flight batch's count as provisional and not surface it as the settled
    "awaiting review" number.
    """
    created = min((r.get("created_at") or "" for r in rows), default="")
    doc = next((r.get("source_document") for r in rows if r.get("source_document")), None)
    in_flight = _extraction_in_flight_for(rows[0]) if rows else False
    # Scope-mismatch verdict, stamped on the rows at extraction when the
    # inferred content scope confidently contradicts the declared hosting_scope.
    # Read from any row (extraction stamps all of them identically); recomputed
    # live, so a set-scope that aligns declared==inferred clears the warning.
    scope_mismatch = next(
        (r.get("content_scope") for r in rows
         if isinstance(r.get("content_scope"), dict)
         and r["content_scope"].get("inferred") != (r.get("hosting_scope") or "both")),
        None,
    )
    return {
        "extraction_batch_id": batch_id,
        "source_document": doc,
        "created_at": created,
        "total": len(rows),
        "needs_review": sum(1 for r in rows if r.get("needs_review")),
        "hosting_scope": next((r.get("hosting_scope") for r in rows if r.get("hosting_scope")), None),
        "scope_mismatch": scope_mismatch,
        # "finalizing" while the run is still extracting/de-duplicating (count is
        # provisional); "pending" once it has settled and is ready for review.
        "in_flight": in_flight,
        "status": "finalizing" if in_flight else "pending",
    }


@router.get("/extraction-batches")
async def list_extraction_batches(document_id: str | None = None):
    """List extraction review batches (groups of proposed rules).

    ``document_id`` filters to batches whose rules were extracted from that
    document (matched via source_document containing the id — the extractor
    sets source_document = "<filename> (<document_id>)"). Without it, returns
    every pending batch. Batches with zero remaining proposed rows have been
    fully approved/rejected and are omitted.
    """
    rows = _proposed_document_rules()
    if document_id:
        rows = [r for r in rows if document_id in (r.get("source_document") or "")]

    by_batch: dict[str, list[dict]] = {}
    for r in rows:
        bid = r.get("extraction_batch_id")
        if not bid:
            continue  # proposed rules without a batch id aren't batches
        by_batch.setdefault(bid, []).append(r)

    batches = [_summarize_batch(bid, rws) for bid, rws in by_batch.items()]
    batches.sort(key=lambda b: b["created_at"], reverse=True)
    return {"batches": batches, "total": len(batches)}


@router.get("/extraction-batches/{batch_id}")
async def get_extraction_batch(batch_id: str):
    """Return one batch's proposed rules, needs_review first for triage."""
    rows = [r for r in _proposed_document_rules()
            if r.get("extraction_batch_id") == batch_id]
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No pending proposals for batch {batch_id}",
        )
    rows.sort(key=lambda r: (not r.get("needs_review"), r.get("created_at") or ""))
    return {**_summarize_batch(batch_id, rows), "rules": rows}


@router.post("/extraction-batches/{batch_id}/approve-all")
async def approve_extraction_batch(
    batch_id: str,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Approve every proposed rule in a batch → flips them active. Loops the
    per-rule approve so behavior is identical; collects per-rule failures
    instead of aborting the whole batch on the first error."""
    rows = [r for r in _proposed_document_rules()
            if r.get("extraction_batch_id") == batch_id]
    if not rows:
        raise HTTPException(status_code=404, detail=f"No pending proposals for batch {batch_id}")
    # All rows share one document/job — block the whole batch while it's still
    # extracting (synthesis/dedup hasn't finalized).
    _guard_not_in_flight(rows[0])
    approved, failed = 0, []
    for r in rows:
        try:
            await approve_standard(r["id"], claims)
            approved += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"batch {batch_id}: approve {r['id']} failed: {e}")
            failed.append(r["id"])
    _audit(claims, "extraction_batch.approve_all", "extraction_batch", batch_id,
           detail={"approved": approved, "failed": len(failed)})
    return {"batch_id": batch_id, "approved": approved, "failed": failed}


@router.post("/extraction-batches/{batch_id}/reject-all")
async def reject_extraction_batch(
    batch_id: str,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Reject (delete) every proposed rule in a batch. Same loop pattern."""
    rows = [r for r in _proposed_document_rules()
            if r.get("extraction_batch_id") == batch_id]
    if not rows:
        raise HTTPException(status_code=404, detail=f"No pending proposals for batch {batch_id}")
    _guard_not_in_flight(rows[0])
    rejected, failed = 0, []
    for r in rows:
        try:
            await reject_standard(r["id"], claims)
            rejected += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"batch {batch_id}: reject {r['id']} failed: {e}")
            failed.append(r["id"])
    _audit(claims, "extraction_batch.reject_all", "extraction_batch", batch_id,
           detail={"rejected": rejected, "failed": len(failed)})
    return {"batch_id": batch_id, "rejected": rejected, "failed": failed}


class SetBatchScopeRequest(BaseModel):
    hosting_scope: Literal["on_prem", "cloud", "both"]


@router.post("/extraction-batches/{batch_id}/set-scope")
async def set_extraction_batch_scope(
    batch_id: str,
    request: SetBatchScopeRequest,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Set hosting_scope on every proposed rule in a batch, in one step.

    hosting_scope is a document-level declaration made at upload and forced onto
    every extracted rule, so a wrong selection mislabels the whole batch. Fixing
    that per-rule would be dozens of edits; this retargets the batch at once,
    before approval. Curator-only, same loop/guard pattern as approve-all.
    """
    rows = [r for r in _proposed_document_rules()
            if r.get("extraction_batch_id") == batch_id]
    if not rows:
        raise HTTPException(status_code=404, detail=f"No pending proposals for batch {batch_id}")
    # Same race as approve/reject: don't retarget mid-extraction, while dedup and
    # cross-reference wiring are still mutating the batch.
    _guard_not_in_flight(rows[0])
    scope = request.hosting_scope
    updated, failed = 0, []
    for r in rows:
        try:
            validation_store.update_standard(r["id"], {
                "hosting_scope": scope,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "edited_by": str((claims or {}).get("email", "") or ""),
            })
            updated += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"batch {batch_id}: set-scope {r['id']} failed: {e}")
            failed.append(r["id"])
    _audit(claims, "extraction_batch.set_scope", "extraction_batch", batch_id,
           detail={"hosting_scope": scope, "updated": updated, "failed": len(failed)})
    return {"batch_id": batch_id, "hosting_scope": scope, "updated": updated, "failed": failed}


@router.get("/{standard_id}")
async def get_standard(standard_id: str):
    """Get a single standard by ID."""
    standard = validation_store.get_standard(standard_id)
    if not standard:
        raise HTTPException(status_code=404, detail=f"Standard {standard_id} not found")
    return standard


@router.post("/{standard_id}/approve")
async def approve_standard(
    standard_id: str,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Approve a proposed standard.

    Behavior depends on proposal_kind:

      add    : flip status to active. This is also the fallback —
               proposals without a kind field land here.
      edit   : copy the proposal's name/description/severity/category/
               validation_criteria onto target_id, then delete the
               proposal row. The target's source/status/source_document
               are preserved.
      delete : flip target_id's status to "archived" (soft delete),
               then delete the proposal row.

    Idempotent only on add (already-active is a no-op). Edit/delete
    proposals are removed once applied, so a re-approve 404s.
    Errors if the standard or its target does not exist.
    """
    standard = validation_store.get_standard(standard_id)
    if not standard:
        raise HTTPException(status_code=404, detail=f"Standard {standard_id} not found")
    _guard_not_in_flight(standard)

    kind = (standard.get("proposal_kind") or "add").lower()
    now_iso = datetime.now(timezone.utc).isoformat()
    actor_email = str((claims or {}).get("email", "") or "")
    # Attribution stamped on the approved rule + an audit event recorded.
    _attr = {"approved_by": actor_email, "approved_at": now_iso}
    _audit(claims, "standard.approve", "standard", standard_id,
           detail={"kind": kind, "target_id": standard.get("target_id")})

    if kind == "add":
        if standard.get("status") == "active":
            return standard  # Already active.
        updated = validation_store.update_standard(
            standard_id, {"status": "active", "updated_at": now_iso, **_attr}
        )
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to approve standard")
        logger.info(f"Approved 'add' proposal {standard_id} by {actor_email or 'unknown'}")
        return updated

    target_id = standard.get("target_id")
    if not target_id:
        raise HTTPException(
            status_code=400,
            detail=f"Proposal {standard_id} has kind={kind} but no target_id",
        )
    target = validation_store.get_standard(target_id)
    if not target:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot apply: target rule {target_id} no longer exists",
        )

    if kind == "edit":
        patched = validation_store.update_standard(
            target_id,
            {
                "name": standard.get("name", target.get("name", "")),
                "description": standard.get("description", target.get("description", "")),
                "severity": standard.get("severity", target.get("severity", "medium")),
                "category": standard.get("category", target.get("category", "general")),
                "validation_criteria": standard.get(
                    "validation_criteria", target.get("validation_criteria", "")
                ),
                "updated_at": now_iso,
                **_attr,
            },
        )
        validation_store.delete_standard(standard_id)
        logger.info(f"Approved 'edit' proposal {standard_id} → patched {target_id}")
        return patched

    if kind == "delete":
        archived = validation_store.update_standard(
            target_id, {"status": "archived", "updated_at": now_iso, **_attr}
        )
        validation_store.delete_standard(standard_id)
        logger.info(f"Approved 'delete' proposal {standard_id} → archived {target_id}")
        return archived

    raise HTTPException(
        status_code=400, detail=f"Unknown proposal_kind on {standard_id}: {kind}"
    )


@router.post("/{standard_id}/reject")
async def reject_standard(
    standard_id: str,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Reject a proposed standard. Equivalent to deleting it.

    Returns 404 if the standard doesn't exist, 409 if it's already
    active (rejecting an approved rule should go through DELETE so the
    intent is unambiguous).
    """
    standard = validation_store.get_standard(standard_id)
    if not standard:
        raise HTTPException(status_code=404, detail=f"Standard {standard_id} not found")
    _guard_not_in_flight(standard)
    if standard.get("status") != "proposed":
        raise HTTPException(
            status_code=409,
            detail="Only proposed standards can be rejected. Use DELETE for active rules.",
        )
    if not validation_store.delete_standard(standard_id):
        raise HTTPException(status_code=500, detail="Failed to reject standard")
    _audit(claims, "standard.reject", "standard", standard_id,
           detail={"name": standard.get("name")})
    logger.info(f"Rejected proposed standard {standard_id}")
    return {"message": f"Standard {standard_id} rejected"}


@router.post("", status_code=201)
async def create_standard(
    request: CreateStandardRequest,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Create a new user-defined standard."""
    now = datetime.now(timezone.utc).isoformat()
    standard = {
        "id": f"HA-U-{uuid.uuid4().hex[:8].upper()}",
        "name": request.name,
        "description": request.description,
        "severity": request.severity,
        "category": request.category,
        "validation_criteria": request.validation_criteria,
        "applies_to_tiers": request.applies_to_tiers,
        "source": StandardSource.USER.value,
        "source_document": None,
        "created_at": now,
        "updated_at": now,
        # Who created it (audit trail).
        "created_by": str((claims or {}).get("email", "") or ""),
        # Required by Pydantic; always present.
        "hosting_scope": request.hosting_scope,
    }
    # OMIT WHEN EMPTY: only attach rule_type / layer / parent_rule_id to
    # the persisted item when they carry a non-empty, non-whitespace
    # value. Empty strings (which JSON.stringify happily produces from a
    # blank <select>) are treated as absence — same as None — so flat
    # rules don't get bloated with null keys and the cache hash
    # projection sees one consistent shape.
    for key in ("rule_type", "layer", "parent_rule_id"):
        val = getattr(request, key)
        if isinstance(val, str):
            val = val.strip() or None
        if val is not None:
            standard[key] = val
    created = validation_store.create_standard(standard)
    _audit(claims, "standard.create", "standard", created["id"],
           detail={"name": created.get("name")})
    logger.info(f"Created user-defined standard: {created['id']}")
    return created


@router.put("/{standard_id}")
async def update_standard(
    standard_id: str,
    request: UpdateStandardRequest,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Update an existing standard (only user-defined ones can be edited).

    Note: PATCH semantics use ``exclude_none=True``, so sending ``null``
    or ``""`` for ``layer`` / ``rule_type`` is treated as 'no change'.
    To clear a previously-set ``layer`` or ``rule_type``, delete and
    recreate the rule. Cleaner three-state PATCH semantics are deferred.
    """
    existing = validation_store.get_standard(standard_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Standard {standard_id} not found")
    # A proposed rule whose extraction run is still finalizing (dedup +
    # cross-reference wiring) must not be edited yet — same race as approve.
    _guard_not_in_flight(existing)

    updates = request.model_dump(exclude_none=True)

    # OMIT WHEN EMPTY: empty / whitespace strings on the structured-rule
    # fields (layer/rule_type/parent_rule_id) are treated as 'not
    # provided'. Strip BEFORE the empty-dict check so a request whose
    # only field was one of those blanks correctly returns 400 rather
    # than slipping past the check and silently no-op-writing only
    # `updated_at`.
    for key in ("rule_type", "layer", "parent_rule_id"):
        if key in updates and isinstance(updates[key], str) and not updates[key].strip():
            updates.pop(key)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    updates["edited_by"] = str((claims or {}).get("email", "") or "")
    updated = validation_store.update_standard(standard_id, updates)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update standard")

    _audit(claims, "standard.edit", "standard", standard_id,
           detail={"fields": sorted(k for k in updates if k not in ("updated_at", "edited_by"))})
    logger.info(f"Updated standard: {standard_id}")
    return updated


@router.delete("/{standard_id}")
async def delete_standard(
    standard_id: str,
    claims: dict = Depends(require_role(Role.CURATOR)),
):
    """Delete a standard (only user-defined ones can be deleted)."""
    existing = validation_store.get_standard(standard_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Standard {standard_id} not found")

    success = validation_store.delete_standard(standard_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete standard")

    _audit(claims, "standard.delete", "standard", standard_id,
           detail={"name": existing.get("name")})
    logger.info(f"Deleted standard: {standard_id}")
    return {"message": f"Standard {standard_id} deleted successfully"}


# In-memory extraction job tracking
_extraction_jobs: dict[str, dict] = {}


async def _extract_in_background(document_id: str, job_id: str, filename: str = ""):
    """Background task for the manual /api/standards/extract route.

    Routed through the unified pipeline
    (agents.extraction.run.run_extraction) — same engine as the auto-trigger
    in documents._trigger_extraction — instead of the older per-format
    _run_extraction_sync (kept but no longer called). Preserves the
    _extraction_jobs bookkeeping the route's status endpoint reads.
    """
    from app.agents.extraction.run import run_extraction
    from app.models.document import SourceFormat
    from app.services.document_store import document_store
    from app.services.extraction_log import (
        is_in_flight, mark_completed, mark_extracting, mark_failed, start_job,
    )
    try:
        document = validation_store.get_document(document_id)
        if document is None:
            raise ValueError(f"Document {document_id} not found in store")

        # Start-time claim (see documents._trigger_extraction). Guards the race
        # the request-side check can't: the upload auto-trigger and this manual
        # run are both background tasks, so one may not have stamped state when
        # the other's request was validated. Checked immediately before
        # start_job with no await between → atomic on this event loop.
        if is_in_flight(document_id):
            logger.info(
                f"Extraction already in flight for {document_id}; skipping duplicate manual run"
            )
            _extraction_jobs[job_id]["status"] = "skipped_duplicate"
            return

        _extraction_jobs[job_id]["status"] = "extracting"
        start_job(document_id, filename or document.filename)
        mark_extracting(document_id)

        pptx_bytes = pdf_bytes = None
        if getattr(document, "source_format", SourceFormat.PDF) == SourceFormat.PPTX:
            if not document.original_s3_key:
                raise ValueError("PPTX document has no original_s3_key")
            pptx_bytes = document_store.download_document(document.original_s3_key)
            try:
                pdf_bytes = document_store.download_document(document.s3_key)
            except Exception:
                logger.warning(f"converted PDF absent for {document_id}; text-only slides")
        else:
            pdf_bytes = document_store.download_document(document.s3_key)

        summary = await run_extraction(
            document, document_store, validation_store,
            pptx_bytes=pptx_bytes, pdf_bytes=pdf_bytes,
        )
        _extraction_jobs[job_id]["status"] = "completed"
        _extraction_jobs[job_id]["extracted_count"] = summary.get("rules_saved", 0)
        _extraction_jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
        mark_completed(document_id, summary.get("rules_saved", 0))
        logger.info(f"Manual extraction complete for {document_id}: {summary}")
    except Exception as e:
        _extraction_jobs[job_id]["status"] = "failed"
        _extraction_jobs[job_id]["error"] = str(e)
        try:
            mark_failed(document_id, str(e))
        except Exception:
            pass
        logger.error(f"Background extraction failed for document {document_id}: {e}")


@router.post("/extract", dependencies=[Depends(require_role(Role.CURATOR))])
async def extract_standards_from_document(
    document_id: str,
    background_tasks: BackgroundTasks,
):
    """Use the Strands extraction agent to extract HA standards from an uploaded standards document.

    This kicks off extraction in the background and returns immediately.
    Poll GET /api/standards/extraction-status to track progress.
    """
    document = validation_store.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    # Refuse to start a second run while one is already going for this document.
    # A standards upload AUTO-triggers extraction, so a manual /extract on top of
    # that (or a double-click) would otherwise mint a SECOND batch with its own
    # id — leaving the reviewer two overlapping proposal sets for one document.
    # Re-extraction after a run COMPLETES is still allowed (the state clears on
    # completion); this blocks only concurrent runs.
    from app.services.extraction_log import is_in_flight
    if is_in_flight(document_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Extraction is already running for this document. Wait for it to "
                "finish before starting another run."
            ),
        )

    job_id = f"ext-{uuid.uuid4().hex[:8]}"
    _extraction_jobs[job_id] = {
        "job_id": job_id,
        "document_id": document_id,
        "filename": document.filename,
        "status": "starting",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "extracted_count": 0,
    }

    background_tasks.add_task(_extract_in_background, document_id, job_id, document.filename)

    return {
        "message": f"Extraction started for document {document_id}.",
        "document_id": document_id,
        "job_id": job_id,
        "status": "starting",
    }
