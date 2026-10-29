# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Vector store service using Bedrock Knowledge Bases for managed RAG."""

import logging
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)


# Bedrock KB occasionally surfaces a generic "Internal Server Exception"
# wrapped as DependencyFailedException — typically a downstream model
# blip that resolves on retry within seconds. Botocore's standard retry
# mode does NOT cover this code, so we add an explicit short retry loop
# inside retrieve_and_generate.
_KB_TRANSIENT_ERROR_CODES = {
    "DependencyFailedException",
    "ThrottlingException",
    "ServiceQuotaExceededException",
    "InternalServerException",
}



def _page_of(metadata: dict) -> int:
    try:
        return int(float(metadata.get("x-amz-bedrock-kb-document-page-number") or 0))
    except (TypeError, ValueError):
        return 0

class TransientKBError(Exception):
    """Raised when retrieve_and_generate hit a transient model failure
    that retries did not recover. Routes can catch this to render a
    user-friendly message instead of the raw boto3 stacktrace.
    """


class VectorStore:
    """Bedrock Knowledge Base vector store for document similarity search.

    Uses the managed Bedrock KB Retrieve API instead of direct OpenSearch access.
    Bedrock KB handles embedding generation, vector storage, and retrieval internally.
    """

    def __init__(self):
        """Initialize vector store with Bedrock KB clients."""
        self._runtime_client = None
        self._agent_client = None
        self._available = False

        if settings.knowledge_base_id:
            try:
                self._runtime_client = boto3.client(
                    'bedrock-agent-runtime', region_name=settings.aws_region
                )
                self._agent_client = boto3.client(
                    'bedrock-agent', region_name=settings.aws_region
                )
                self._available = True
                logger.info(
                    f"Bedrock Knowledge Base configured: {settings.knowledge_base_id}"
                )
            except Exception as e:
                logger.warning(f"Failed to initialize Bedrock KB clients: {e}")
                self._available = False
        else:
            logger.info("Knowledge Base ID not configured. Vector search unavailable.")

    def index_document_chunks(
        self,
        document_id: str,
        chunks: list[dict],
    ) -> int:
        """
        Trigger Knowledge Base sync to index new document content.

        With Bedrock KB, documents are indexed by triggering an ingestion job
        rather than manually indexing individual chunks.

        Args:
            document_id: Document identifier
            chunks: List of dicts (not used directly - KB handles chunking)

        Returns:
            Number of chunks (KB will re-chunk during ingestion)
        """
        if not self._available:
            logger.info(
                f"KB not configured. Skipping indexing for document {document_id}"
            )
            return len(chunks)

        # Trigger a data source sync so Bedrock KB processes the new document
        self._trigger_sync()
        logger.info(
            f"KB sync triggered for document {document_id} ({len(chunks)} chunks provided)"
        )
        return len(chunks)

    def similarity_search(
        self,
        query: str,
        document_id: str,
        top_k: int = 5,
    ) -> list[dict]:
        """
        Search for similar chunks using Bedrock Knowledge Base retrieval.

        Used by the kb_search tool (agents/tools/kb_tools.py), which the
        query agent calls for free-form Q&A over the corpus.

        Args:
            query: Search query
            document_id: Document to search within
            top_k: Number of results to return

        Returns:
            List of matching chunks with scores
        """
        if not self._available:
            logger.info(f"KB not available, returning empty results for: {query[:50]}")
            return []

        try:
            retrieval_config = {
                'vectorSearchConfiguration': {
                    'numberOfResults': top_k
                }
            }

            # Filter by document if specified
            if document_id:
                retrieval_config['vectorSearchConfiguration']['filter'] = {
                    'equals': {
                        'key': 'x-amz-bedrock-kb-source-uri',
                        'value': f"s3://*/documents/{document_id}/"
                    }
                }

            response = self._runtime_client.retrieve(
                knowledgeBaseId=settings.knowledge_base_id,
                retrievalQuery={'text': query},
                retrievalConfiguration=retrieval_config,
            )

            results = []
            for result in response.get('retrievalResults', []):
                results.append({
                    'text': result.get('content', {}).get('text', ''),
                    'score': result.get('score', 0.0),
                    'source_uri': (
                        result.get('location', {})
                        .get('s3Location', {})
                        .get('uri', '')
                    ),
                    'section_title': '',
                    # Bedrock records the page for PDF sources; without it a
                    # citation can only name the document.
                    'page_number': _page_of(result.get('metadata') or {}),
                })

            logger.info(f"KB retrieved {len(results)} results for query: {query[:50]}")
            return results

        except Exception as e:
            logger.error(f"KB similarity search failed: {e}")
            return []

    # NOTE: delete_document_chunks() was removed — zero callers across
    # app/ and agents/.

    def retrieve_and_generate(self, query: str, model_arn: str) -> dict:
        """Use Bedrock KB retrieve_and_generate for full RAG answer.

        Retries up to 2 times on transient Bedrock errors
        (DependencyFailedException, ThrottlingException, etc.) with
        a short backoff. If all retries fail, raises TransientKBError
        so the route handler can return a user-friendly message
        instead of the raw stacktrace.
        """
        if not settings.knowledge_base_id:
            return {"output": {"text": "Knowledge Base not configured."}, "citations": []}

        config = {
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": settings.knowledge_base_id,
                "modelArn": model_arn,
                "generationConfiguration": {
                    "promptTemplate": {
                        "textPromptTemplate": (
                            "You are an AI assistant that validates infrastructure designs against "
                            "High Availability standards. Use the following context to answer accurately. "
                            "Cite specific details from the documents.\n\n"
                            "Context:\n$search_results$\n\n"
                            "Question: $query$\n\n"
                            "Answer:"
                        )
                    }
                },
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {
                        "numberOfResults": 5
                    }
                },
            },
        }

        max_attempts = 3
        backoff_s = 1.0
        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                return self._runtime_client.retrieve_and_generate(
                    input={"text": query},
                    retrieveAndGenerateConfiguration=config,
                )
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                last_error = e
                if code in _KB_TRANSIENT_ERROR_CODES and attempt < max_attempts:
                    logger.warning(
                        f"KB retrieve_and_generate transient error "
                        f"({code}) on attempt {attempt}/{max_attempts}; "
                        f"retrying in {backoff_s:.1f}s"
                    )
                    time.sleep(backoff_s)  # nosemgrep: arbitrary-sleep — intentional exponential backoff between KB retry attempts
                    backoff_s *= 2
                    continue
                logger.error(f"Retrieve and generate failed: {e}")
                if code in _KB_TRANSIENT_ERROR_CODES:
                    raise TransientKBError(
                        "The AI service had a transient error. "
                        "Please try again in a few seconds."
                    ) from e
                raise
            except Exception as e:
                # Non-ClientError surface unchanged — these are usually
                # auth or config failures that won't recover on retry.
                logger.error(f"Retrieve and generate failed: {e}")
                raise

        # Defensive — loop above always returns or raises.
        if last_error:
            raise TransientKBError(
                "The AI service had a transient error. "
                "Please try again in a few seconds."
            ) from last_error
        return {"output": {"text": "No response."}, "citations": []}

    def _trigger_sync(self):
        """Trigger a Bedrock Knowledge Base data source ingestion job."""
        if not settings.data_source_id:
            logger.debug("DATA_SOURCE_ID not configured, skipping KB sync")
            return

        try:
            response = self._agent_client.start_ingestion_job(
                knowledgeBaseId=settings.knowledge_base_id,
                dataSourceId=settings.data_source_id,
            )
            job_id = (
                response.get('ingestionJob', {}).get('ingestionJobId', 'unknown')
            )
            logger.info(f"KB ingestion job started: {job_id}")
        except Exception as e:
            logger.warning(f"Failed to trigger KB sync: {e}")


# Singleton instance
vector_store = VectorStore()
