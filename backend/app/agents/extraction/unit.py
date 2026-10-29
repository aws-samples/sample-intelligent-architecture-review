# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The unified `Unit` abstraction for extraction.

Both PDF pages and PPTX slides normalise to a `Unit`: the format-agnostic
thing the extraction agent reasons over. This is what lets us collapse the
two divergent extractors (extraction_agent.py for PDF, extraction_agent_pptx.py
for PPTX) into one multimodal pipeline.

Design intent:
  - Carry BOTH text AND structured tables AND the full-page image so the
    model sees everything in one multimodal turn — no separate Vision tool
    call, no TSV-flattened tables.
  - Structured tables are the actual grid (list of rows of cells), NOT the
    tab-joined blob the old pptx path passed. The table specialist and the
    prompt builder consume this grid directly.
  - The rendered PNG is the full page/slide raster (PPTX: rasterise; PDF:
    render_pages_to_png) so vector diagrams — whose edges live only in
    pixels — are visible. Bytes are carried lazily via `image_ref` (an S3
    key or local handle) resolved at call time to avoid holding 48 PNGs in
    memory at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UnitTable:
    """One table on a unit, as a structured grid (rows of cell strings).

    `ragged` (rows of differing length) signals merged cells / multi-row
    headers — the structure the old TSV flattening destroyed.
    """
    rows: list[list[str]] = field(default_factory=list)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    @property
    def ragged(self) -> bool:
        return len({len(r) for r in self.rows}) > 1

    def to_markdown(self) -> str:
        """Render as a GitHub-flavoured markdown table so the model sees
        real column structure instead of an ambiguous tab blob. First row
        is treated as the header; ragged rows are padded."""
        if not self.rows:
            return ""
        width = self.n_cols
        def row(cells: list[str]) -> str:
            padded = [c.replace("|", "\\|").replace("\n", " ") for c in cells]
            padded += [""] * (width - len(padded))
            return "| " + " | ".join(padded) + " |"
        header = self.rows[0]
        sep = "| " + " | ".join(["---"] * width) + " |"
        body = [row(r) for r in self.rows[1:]]
        return "\n".join([row(header), sep, *body])


@dataclass
class Unit:
    """One page (PDF) or slide (PPTX) — the atomic extraction unit."""

    index: int                      # 1-based page/slide number
    source_format: str              # "pdf" | "pptx"
    text: str = ""                  # native text (headings + prose + labels)
    tables: list[UnitTable] = field(default_factory=list)
    image_ref: Optional[str] = None  # S3 key (or local key) for the full-page PNG
    classification: str = "hybrid"   # title_only | text_heavy | diagram_heavy | hybrid
    content_kind: str = "filled"     # filled | instruction | placeholder
    # Diagnostics (populated at ingestion; used for routing + gating).
    n_shapes: int = 0
    n_pictures: int = 0
    n_diagram_shapes: int = 0        # autoshapes/freeforms — topology candidates

    @property
    def has_tables(self) -> bool:
        return bool(self.tables)

    @property
    def is_diagram(self) -> bool:
        return self.n_diagram_shapes >= 4 or self.n_pictures > 0

    @property
    def label(self) -> str:
        noun = "slide" if self.source_format == "pptx" else "page"
        return f"{noun} {self.index}"

    def tables_as_markdown(self) -> str:
        """All tables rendered as markdown, separated by blank lines."""
        return "\n\n".join(t.to_markdown() for t in self.tables if t.rows)
