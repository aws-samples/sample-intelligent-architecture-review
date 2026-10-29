# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Ingestion — normalise PDF pages and PPTX slides into unified `Unit`s.

This is the single front-door that replaces the format-divergent parsing in
extraction_agent.py (PDF, text-only) and extraction_agent_pptx.py (PPTX). Both
formats produce a `list[Unit]` carrying text + structured tables + a full-page
image reference, so downstream extraction is format-agnostic.

Reuses existing, proven components rather than reparsing:
  - PPTX: app.services.pptx_converter.parse_pptx_native (structured tables +
    classification already computed) and rasterise_slides_to_s3 (144-DPI PNGs).
  - PDF:  app.utils.pdf_parser (sections + per-page find_tables) and
    render_pages_to_png (full-page raster — previously validator-only; wiring
    it into extraction is the fix for PDF diagram-blindness).
"""

from __future__ import annotations

import logging

from app.agents.extraction.unit import Unit, UnitTable

logger = logging.getLogger(__name__)


def _rasterise_pdf_to_slide_keys(pdf_bytes: bytes, document_id: str, doc_store,
                                 dpi: int = 144) -> None:
    """Render each PDF page to a PNG at documents/{id}/slides/{N}.png.

    Mirrors pptx_converter.rasterise_slides_to_s3 but takes bytes directly
    (the converted PDF we already have) so no sidecar round-trip is needed.
    """
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        for i in range(doc.page_count):
            pix = doc.load_page(i).get_pixmap(matrix=matrix, alpha=False)
            key = f"documents/{document_id}/slides/{i + 1}.png"
            doc_store.upload_document(pix.tobytes("png"), key, content_type="image/png")
    finally:
        doc.close()


def ingest_pptx(pptx_bytes: bytes, document_id: str, doc_store,
                converted_pdf_bytes: bytes | None = None) -> list[Unit]:
    """Build Units from a PPTX deck.

    Native text + structured tables come from parse_pptx_native. Slide PNGs
    (144 DPI) are guaranteed to exist here — the upload route rasterises them
    in a SEPARATE background task that races this extraction trigger, so we
    can't assume they're present. Since multimodal is the whole point, if the
    PNGs are missing we rasterise them locally from the already-converted PDF
    (document.s3_key) using PyMuPDF — no sidecar, no re-conversion.
    """
    from app.services.pptx_converter import parse_pptx_native

    meta = parse_pptx_native(pptx_bytes)

    image_keys: dict[int, str] = {
        s.slide_index: f"documents/{document_id}/slides/{s.slide_index}.png"
        for s in meta.slides
    }

    # Ensure PNGs exist: probe slide 1; if absent, rasterise the whole deck
    # from the converted PDF. Best-effort — failure degrades to text-only.
    try:
        need_render = True
        try:
            doc_store.download_document(image_keys[1])
            need_render = False  # upload task already produced them
        except Exception:
            need_render = True
        if need_render and converted_pdf_bytes:
            logger.info(f"slide PNGs absent for {document_id}; rendering from converted PDF")
            _rasterise_pdf_to_slide_keys(converted_pdf_bytes, document_id, doc_store)
        elif need_render:
            logger.warning(f"no converted PDF for {document_id}; slides text-only")
    except Exception:
        logger.exception("pptx slide rasterisation failed; continuing text-only")

    units: list[Unit] = []
    for s in meta.slides:
        units.append(Unit(
            index=s.slide_index,
            source_format="pptx",
            text=s.native_text,
            tables=[UnitTable(rows=grid) for grid in s.tables],
            image_ref=image_keys.get(s.slide_index),
            classification=s.slide_type.value,
            n_shapes=s.shape_count,
            n_pictures=s.image_count,
            # parse_pptx_native doesn't separate diagram shapes; approximate
            # from shape_count minus tables/pictures.
            n_diagram_shapes=max(0, s.shape_count - s.table_count - s.image_count),
        ))
    return units


def ingest_pdf(pdf_bytes: bytes, document_id: str, doc_store) -> list[Unit]:
    """Build Units from a PDF, one per page.

    Wires render_pages_to_png into extraction (previously validator-only) so
    the full-page raster — including vector diagrams that emit no embedded
    image — is available to the model. Structured tables come from
    PyMuPDF find_tables().extract() (the raw grid, not the markdown string
    extract_page_tables returns).
    """
    import fitz
    from app.utils.pdf_parser import extract_text_and_sections, render_pages_to_png

    parsed = extract_text_and_sections(pdf_bytes)

    # Per-page structured tables via a direct fitz pass (keeps the grid).
    tables_by_page: dict[int, list[UnitTable]] = {}
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            for i in range(doc.page_count):
                page = doc.load_page(i)
                grids: list[UnitTable] = []
                try:
                    for tbl in page.find_tables().tables:
                        rows = tbl.extract()
                        if rows:
                            grids.append(UnitTable(rows=[
                                [(c or "") for c in row] for row in rows
                            ]))
                except Exception:
                    pass
                if grids:
                    tables_by_page[i + 1] = grids
        finally:
            doc.close()
    except Exception:
        logger.exception("pdf table extraction failed; continuing without grids")

    # Render full-page PNGs and upload so extraction is multimodal.
    image_keys: dict[int, str] = {}
    try:
        pngs = render_pages_to_png(pdf_bytes, dpi=150)
        for i, png in enumerate(pngs, start=1):
            key = f"documents/{document_id}/pages/{i}.png"
            try:
                doc_store.upload_document(png, key, content_type="image/png")
                image_keys[i] = key
            except Exception:
                logger.exception(f"page {i} png upload failed; text-only for it")
    except Exception:
        logger.exception("render_pages_to_png failed; extraction runs text-only")

    # One Unit per section (sections are page-anchored in this codebase).
    units: list[Unit] = []
    for sec in parsed.sections:
        pg = sec.page_number
        units.append(Unit(
            index=pg,
            source_format="pdf",
            text=f"{sec.title}\n{sec.content}" if sec.title else sec.content,
            tables=tables_by_page.get(pg, []),
            image_ref=image_keys.get(pg),
            classification="pdf_section",
            n_pictures=len(getattr(sec, "images", []) or []),
        ))
    return units


def ingest(document, doc_store, *, pptx_bytes: bytes | None = None,
           pdf_bytes: bytes | None = None) -> list[Unit]:
    """Dispatch to the right ingester based on the document's source_format."""
    from app.models.document import SourceFormat

    fmt = getattr(document, "source_format", SourceFormat.PDF)
    if fmt == SourceFormat.PPTX:
        if pptx_bytes is None:
            raise ValueError("PPTX ingestion requires pptx_bytes")
        # For PPTX, pdf_bytes (when provided) is the CONVERTED PDF, used only
        # to rasterise slides locally if the upload task hasn't yet.
        return ingest_pptx(pptx_bytes, document.document_id, doc_store,
                           converted_pdf_bytes=pdf_bytes)
    if pdf_bytes is None:
        raise ValueError("PDF ingestion requires pdf_bytes")
    return ingest_pdf(pdf_bytes, document.document_id, doc_store)
