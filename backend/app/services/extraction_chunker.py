# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document chunking for rule extraction.

Rule extraction originally used a single Strands agent loop for the
whole document. Comprehensive specs (~80+ rules) blew the per-call
output ceiling and Strands' MaxTokensReachedException tore down the run.

This module partitions a parsed Document into page-range chunks so
the extraction loop runs once per chunk. Each chunk produces ~10-25
rules, well under any per-call ceiling. Output rules from all chunks
are merged + deduped at the end.

Design decisions (locked):
- Chunk size: PAGES_PER_CHUNK pages (default 8, env overridable).
- Chunks are contiguous page ranges, not section-aware. We could be
  smarter (split on section boundaries) but pages are reliable and
  ParsedSection.page_number is what's available everywhere.
- One image's nearby_section anchors to a heading; chunk-aware image
  filtering would require the per-image enrichment pass to know which
  chunks "owned" which rules. We keep that image set untouched and
  run against the merged rule set (the natural home for cross-chunk
  diagrams).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.document import Document, ParsedSection

logger = logging.getLogger(__name__)

# Env-overridable. Eight pages tracks the typical rule
# density of a matrix-heavy standards document.
DEFAULT_PAGES_PER_CHUNK = int(os.getenv("EXTRACTION_PAGES_PER_CHUNK", "8"))


@dataclass
class Chunk:
    """One page-range slice of a Document. Each chunk owns its own
    text payload (the agent reads it directly from the prompt) and a
    list of ParsedSection objects for any callers that need
    structured access.
    """
    index: int          # 1-based, for live-log readability
    total: int          # total chunks for this document
    start_page: int     # 1-based, inclusive
    end_page: int       # 1-based, inclusive
    sections: list = field(default_factory=list)  # list[ParsedSection]
    text: str = ""

    @property
    def label(self) -> str:
        return f"chunk {self.index} of {self.total} (pages {self.start_page}-{self.end_page})"


def _section_page(section: "ParsedSection") -> int:
    """ParsedSection.page_number is the start page; sections may
    span multiple pages but the model treats page-number as the
    anchor for chunking."""
    return getattr(section, "page_number", 0) or 0


def chunk_document(
    document: "Document",
    pages_per_chunk: int = DEFAULT_PAGES_PER_CHUNK,
) -> list[Chunk]:
    """Partition a Document's parsed sections into page-range chunks.

    Returns at least one chunk for any document with sections. Empty
    or unparsed documents return one all-encompassing chunk so the
    caller can fall through to the existing whole-document flow.
    """
    sections = list(getattr(document, "sections", None) or [])

    if not sections:
        # Unparsed or pre-parsing — single chunk covering whatever
        # the doc has. The agent will still call get_document_text
        # in this case, which is the whole-document behaviour.
        return [
            Chunk(
                index=1,
                total=1,
                start_page=1,
                end_page=getattr(document, "page_count", 1) or 1,
                sections=[],
                text="",
            )
        ]

    pages_per_chunk = max(1, int(pages_per_chunk))

    # Determine the document's page span from sections. Use min/max
    # rather than count() because a section may cover multiple pages.
    page_numbers = [_section_page(s) for s in sections if _section_page(s) > 0]
    if not page_numbers:
        # Sections exist but no page numbers — collapse to one chunk.
        return [
            Chunk(
                index=1,
                total=1,
                start_page=1,
                end_page=getattr(document, "page_count", 1) or 1,
                sections=sections,
                text=_render_sections(sections),
            )
        ]
    first_page = min(page_numbers)
    last_page = max(page_numbers)

    chunks: list[Chunk] = []
    cur_start = first_page
    while cur_start <= last_page:
        cur_end = min(cur_start + pages_per_chunk - 1, last_page)
        chunk_sections = [
            s for s in sections
            if cur_start <= _section_page(s) <= cur_end
        ]
        chunks.append(
            Chunk(
                index=len(chunks) + 1,
                total=0,  # filled in after the loop
                start_page=cur_start,
                end_page=cur_end,
                sections=chunk_sections,
                text=_render_sections(chunk_sections),
            )
        )
        cur_start = cur_end + 1

    # Backfill the running total now that we know it.
    total = len(chunks)
    for c in chunks:
        c.total = total

    logger.info(
        f"Chunked document {getattr(document, 'document_id', '?')} "
        f"into {total} chunk{'s' if total != 1 else ''} "
        f"of up to {pages_per_chunk} pages each"
    )
    return chunks


def _render_sections(sections: list) -> str:
    """Format a list of ParsedSection into a flat agent-prompt
    string. Mirrors the format `get_document_text` produces so the
    agent sees the same shape it's used to."""
    parts: list[str] = []
    for s in sections:
        title = getattr(s, "title", "") or ""
        content = getattr(s, "content", "") or ""
        page_number = getattr(s, "page_number", 0) or 0
        parts.append(f"[Page {page_number}] {title}\n{content}")
    return "\n\n".join(parts)


def dedup_rules(rules: list[dict]) -> tuple[list[dict], list[str]]:
    """Dedupe rules across chunks.

    Deduplication key: (lowered+stripped name, sorted tiers tuple,
    layer string). The first occurrence wins; subsequent duplicates
    are dropped and their ids returned for logging.

    Returns ``(kept, dropped_ids)``.
    """
    seen: dict[tuple, dict] = {}
    dropped: list[str] = []
    for r in rules:
        name_key = (r.get("name") or "").strip().lower()
        tiers = r.get("applies_to_tiers") or []
        if isinstance(tiers, list):
            tiers_key = tuple(sorted(int(t) for t in tiers if isinstance(t, (int, str))))
        else:
            tiers_key = tuple()
        layer_key = (r.get("layer") or "").strip().lower()
        key = (name_key, tiers_key, layer_key)
        if key in seen:
            dropped.append(r.get("id") or "?")
            continue
        seen[key] = r
    return list(seen.values()), dropped
