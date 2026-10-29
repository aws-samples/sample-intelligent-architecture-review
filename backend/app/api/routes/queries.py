# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Query routes - Natural language queries over documents."""

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_validation_store, get_vector_store
from app.bedrock_models import QUERY_REASONING_BUDGET as _QUERY_REASONING_BUDGET
from app.models.query import QueryRequest, QueryResponse, SourceReference
from app.services.validation_store import ValidationStore
from app.services.vector_store import TransientKBError, VectorStore


logger = logging.getLogger(__name__)

router = APIRouter()

# Extended-thinking budget lives in app.bedrock_models alongside the model tier
# constants, so inference settings are declared in one place rather than at the
# call site.


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Events frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class _QueueCallbackHandler:
    """Strands callback that forwards agent events onto an asyncio queue.

    Runs inside the executor thread (the agent is blocking), so it hops
    each event back onto the event loop with ``call_soon_threadsafe``.
    Mirrors LiveLogCallbackHandler's event filtering but targets an SSE
    stream instead of the extraction live-log:

      - reasoning deltas → ("thinking", text)
      - assistant text   → ("delta", text)
      - unique tool calls→ ("tool", "<name> — <hint>")

    Never raises into the agent loop — a callback crash must not kill the
    run, so every dispatch is guarded.
    """

    def __init__(self, loop, queue: asyncio.Queue):
        self._loop = loop
        self._queue = queue
        self._prev_tool_use = None
        self._tool_count = 0

    def _emit(self, kind: str, payload) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (kind, payload))

    def __call__(self, **kwargs) -> None:
        try:
            # Reasoning (extended thinking) deltas.
            if kwargs.get("reasoning") and kwargs.get("reasoningText"):
                self._emit("thinking", kwargs["reasoningText"])
                return

            # Assistant answer text.
            data = kwargs.get("data")
            if data:
                self._emit("delta", data)

            # Tool invocations — one line per unique tool use, matching the
            # LiveLogCallbackHandler de-dup approach.
            current_tool_use = kwargs.get("current_tool_use") or {}
            if current_tool_use and current_tool_use.get("name"):
                if self._prev_tool_use != current_tool_use:
                    self._prev_tool_use = current_tool_use
                    self._tool_count += 1
                    name = current_tool_use.get("name", "tool")
                    tool_input = current_tool_use.get("input") or {}
                    hint = ""
                    if isinstance(tool_input, dict):
                        q = tool_input.get("query") or tool_input.get("document_id")
                        if q:
                            hint = f" — {str(q)[:80]}"
                    self._emit("tool", f"{name}{hint}")
        except Exception:  # noqa: BLE001 — a callback must never crash the agent
            logger.exception("query stream callback error — swallowed")


@router.post("")
async def create_query(
    request: QueryRequest,
    store: ValidationStore = Depends(get_validation_store),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Submit a natural language query about documents.

    If document_id is provided, queries against that specific document.
    If omitted, queries across all indexed documents via Bedrock Knowledge Base.
    """
    start_time = time.time()

    if request.document_id:
        # Query specific document using Strands agent
        document = store.get_document(request.document_id)
        if not document:
            raise HTTPException(
                status_code=404,
                detail=f"Document {request.document_id} not found",
            )

        # status may be a DocumentStatus enum (in-memory) or a plain str (loaded
        # from DynamoDB), so normalise before comparing — same defensive pattern
        # as app/api/routes/standards.py.
        doc_status = document.status.value if hasattr(document.status, "value") else str(document.status)
        if doc_status not in ("parsed", "analyzed", "ready"):
            raise HTTPException(
                status_code=400,
                detail=f"Document must be parsed before querying. Current status: {doc_status}",
            )

        try:
            prompt = (
                f"Answer the following question about document {request.document_id}: "
                f"{request.question}"
            )
            from app.agents.query_agent import create_query_agent

            agent = create_query_agent()
            result = agent(prompt)

            processing_time = int((time.time() - start_time) * 1000)

            response = QueryResponse(
                query_id=f"qry-{uuid.uuid4().hex[:12]}",
                document_id=request.document_id,
                question=request.question,
                answer=str(result),
                sources=[],
                confidence=0.8,
                processing_time_ms=processing_time,
            )
            return response.model_dump(mode="json")
        except Exception as e:
            logger.error(f"Query processing failed: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to process query: {str(e)}")
    else:
        # Query across all documents via Knowledge Base
        try:
            # Bedrock cross-region inference profile ARNs are
            # account-scoped. Resolve at runtime so the same code
            # works in any account where this service is deployed.
            from app.config import settings
            import boto3
            account_id = boto3.client("sts").get_caller_identity()["Account"]
            region = settings.aws_region
            model_id = settings.bedrock_model_id
            model_arn = (
                f"arn:aws:bedrock:{region}:{account_id}:"
                f"inference-profile/{model_id}"
            )
            kb_response = vector_store.retrieve_and_generate(
                query=request.question,
                model_arn=model_arn,
            )

            processing_time = int((time.time() - start_time) * 1000)

            sources = []
            for citation in kb_response.get("citations", []):
                for ref in citation.get("retrievedReferences", []):
                    uri = ref.get("location", {}).get("s3Location", {}).get("uri", "")
                    text = ref.get("content", {}).get("text", "")[:200]
                    sources.append(
                        SourceReference(
                            section_title=uri.split("/")[-1] if uri else "Unknown",
                            page_number=0,
                            relevance_score=0.9,
                            excerpt=text,
                        )
                    )

            response = QueryResponse(
                query_id=str(uuid.uuid4()),
                document_id="all",
                question=request.question,
                answer=kb_response.get("output", {}).get("text", "No answer generated."),
                sources=sources[:5],
                confidence=0.9,
                processing_time_ms=processing_time,
            )

            # Return directly without saving to DynamoDB (avoids float/Decimal issue)
            return response.model_dump(mode="json")

        except TransientKBError as e:
            logger.warning(f"KB query failed transiently: {e}")
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            logger.error(f"KB query failed: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to query knowledge base: {str(e)}"
            )


@router.post("/stream")
async def create_query_stream(
    request: QueryRequest,
    store: ValidationStore = Depends(get_validation_store),
):
    """Streaming (SSE) variant of create_query using the Strands query agent.

    Unlike POST /queries (which for the cross-document case does a single
    KB retrieve_and_generate), this always runs the tool-using agent so the
    client can watch the reasoning: ``event: thinking`` (extended thinking),
    ``event: tool`` (each kb_search / calculate / etc. as it fires), and
    ``event: delta`` (the answer, token by token). Terminates with
    ``event: done`` or ``event: error``.

    The agent has kb_search, so it handles both document-scoped and
    cross-document questions — document_id, when supplied, just focuses the
    prompt. Frames set ``Cache-Control: no-transform`` so CloudFront doesn't
    buffer them.
    """
    from fastapi.responses import StreamingResponse

    if request.document_id:
        document = store.get_document(request.document_id)
        if not document:
            raise HTTPException(
                status_code=404,
                detail=f"Document {request.document_id} not found",
            )
        if document.status.value not in ("parsed", "analyzed", "ready"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Document must be parsed before querying. "
                    f"Current status: {document.status.value}"
                ),
            )
        prompt = (
            f"Answer the following question about document {request.document_id}: "
            f"{request.question}"
        )
    else:
        prompt = request.question

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _run():
            """Produce agent events onto the queue off the event loop.

            Emits ((thinking|delta|tool), text) tuples from the in-process
            Strands agent via a callback handler.
            """
            try:
                from app.agents.query_agent import create_query_agent

                handler = _QueueCallbackHandler(loop, queue)
                agent = create_query_agent(
                    callback_handler=handler,
                    reasoning_budget=_QUERY_REASONING_BUDGET,
                )
                agent(prompt)
            except Exception as exc:  # noqa: BLE001 — surfaced as an SSE error
                logger.exception("query agent stream failed")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("eos", None))

        loop.run_in_executor(None, _run)

        got_error = False
        while True:
            kind, payload = await queue.get()
            if kind == "eos":
                break
            elif kind == "thinking":
                yield _sse("thinking", {"text": payload})
            elif kind == "tool":
                yield _sse("tool", {"text": payload})
            elif kind == "delta":
                yield _sse("delta", {"text": payload})
            elif kind == "error":
                got_error = True
                yield _sse("error", {"detail": f"Query failed: {payload}"})
                # keep draining until eos so the worker thread finishes
        if not got_error:
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


