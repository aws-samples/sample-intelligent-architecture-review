# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Knowledge base search tools for Strands agents."""

import re

from strands.tools import tool


@tool
def kb_search(query: str, num_results: int = 5) -> str:
    """Search the knowledge base for relevant document content matching the query.

    Every result names the document it came from (and its page, for PDFs).
    Attribute each claim to that document only."""
    from app.services.vector_store import vector_store

    try:
        results = vector_store.similarity_search(query, document_id="", top_k=num_results)
        if not results:
            return "No relevant results found."
        # Without the source on each passage the model could only attribute
        # content to the document named in the question, and it did: another
        # design's risk register was presented, with invented page numbers, as
        # the one asked about.
        parts = [
            "Each result below names its source document. Attribute every claim "
            "to the source it came from; never to a different document."
        ]
        for i, r in enumerate(results):
            source = describe_source(r.get("source_uri", ""))
            page = r.get("page_number") or 0
            where = f"{source}, page {page}" if page else source
            title = (r.get("section_title") or "").strip()
            label = f"{title} — Source: {where}" if title else f"Source: {where}"
            parts.append(
                f"[Result {i+1}] {label} (Score: {r.get('score', 0):.2f})\n"
                f"{r.get('text', '')[:500]}"
            )
        return "\n\n".join(parts)
    except Exception as e:
        return f"Search failed: {str(e)}"


_DOC_URI = re.compile(r"/documents/(doc-[0-9a-z]+)/([^/]+)$")
_VALIDATION_URI = re.compile(r"/validations/(val-[0-9a-z]+)/")


def describe_source(uri: str) -> str:
    """A reader-facing name for a knowledge-base source URI.

    The bucket holds the uploaded file itself, its parsed text, and validation
    findings; name each so the model can tell a design's own words from a
    finding about it."""
    if not uri:
        return "unknown source"
    match = _VALIDATION_URI.search(uri)
    if match:
        return f"validation findings {match.group(1)}"
    match = _DOC_URI.search(uri)
    if not match:
        return uri.rsplit("/", 1)[-1] or uri
    doc_id, name = match.groups()
    if name.lower().endswith((".pdf", ".pptx")):
        return f"{name} ({doc_id})"
    try:
        from app.services.validation_store import validation_store
        document = validation_store.get_document(doc_id)
        if document and document.filename:
            return f"{document.filename} ({doc_id}), parsed text"
    except Exception:
        pass
    return f"{doc_id}, parsed text"


@tool
def get_validation_results_summary() -> str:
    """Get a summary of all validation results across all documents."""
    from app.services.validation_store import validation_store

    validations = validation_store.list_validations()
    if not validations:
        return "No validations have been run yet."
    parts = []
    for v in validations:
        doc = validation_store.get_document(v.document_id)
        doc_name = doc.filename if doc else v.document_id
        findings_summary = (
            f"{len(v.findings)} findings" if v.findings else "no findings"
        )
        parts.append(
            f"- {doc_name}: score={v.overall_score}, {findings_summary}, status={v.status}"
        )
    return f"{len(validations)} validations:\n" + "\n".join(parts)
