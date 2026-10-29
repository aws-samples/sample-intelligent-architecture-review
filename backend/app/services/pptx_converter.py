# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PPTX → PDF conversion + native slide metadata extraction.

This module owns three concerns:

1. **Native slide parsing** (pure-Python, runs in the API container).
   `parse_pptx_native` reads a `.pptx` byte string and returns a
   `SlideMetadata` per slide: native text, table data, shape and
   image counts. Used for verbatim rule wording (`text_heavy`
   slides) and for the heuristic classifier.

2. **Slide-type classification.** `classify_slide` applies the four
   slide-type thresholds (title_only / text_heavy /
   diagram_heavy / hybrid). Misclassifications are non-fatal — both
   text and image are sent to the model on every slide; the type
   only chooses which prompt template to use.

3. **Sidecar conversion client.** `convert_pptx_to_pdf` is a thin
   httpx wrapper around the `pptx-converter` sidecar service. The
   sidecar runs LibreOffice headless and is reached via Cloud Map
   service-discovery DNS at the URL in env `PPTX_CONVERTER_URL`.
   The API never touches LibreOffice itself — the sidecar isolates
   the ~400 MB of binaries with no AWS credentials of its own.
   Bytes do NOT cross the API↔sidecar HTTP boundary; instead each
   side reads/writes via presigned S3 URLs supplied in the
   request body.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.models.document import SlideType

logger = logging.getLogger(__name__)


# Default slide-classification thresholds.
# Surfaced as module-level constants so tests can override them
# without poking through the function body.
TITLE_TEXT_MAX = 80
TITLE_SHAPE_MAX = 3
TEXT_HEAVY_TEXT_MIN = 400
TEXT_HEAVY_SHAPE_MAX = 6
DIAGRAM_HEAVY_TEXT_MAX = 200
DIAGRAM_HEAVY_SHAPE_MIN = 8


# Sidecar config. In every real deploy PPTX_CONVERTER_URL is wired by
# Terraform via Cloud Map service-discovery DNS
# (pptx-converter.<project>-<env>.local); this fallback is only for local
# runs/tests, so it points at localhost rather than baking in a project name.
DEFAULT_CONVERTER_URL = "http://localhost:8001"
SIDECAR_TIMEOUT_S = 90  # connect + read; sidecar's own subprocess timeout is 60s


@dataclass
class SlideNative:
    """Per-slide native metadata extracted via python-pptx.

    Counts exclude the slide background and master-layout shapes;
    only what the deck author placed on this specific slide.
    """

    slide_index: int  # 1-based
    native_text: str
    tables: list[list[list[str]]] = field(default_factory=list)
    shape_count: int = 0
    image_count: int = 0
    table_count: int = 0
    slide_type: SlideType = SlideType.HYBRID  # populated by classify_slide


@dataclass
class SlideMetadata:
    """Result of parse_pptx_native — one entry per slide."""

    slides: list[SlideNative]

    @property
    def slide_count(self) -> int:
        return len(self.slides)


def _shape_text(shape) -> str:
    """Return the text content of a shape, recursing into groups."""
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        # Group shapes don't have text directly — descend.
        return "\n".join(_shape_text(s) for s in shape.shapes)
    if not getattr(shape, "has_text_frame", False):
        return ""
    if shape.text_frame is None:
        return ""
    return shape.text_frame.text or ""


def _shape_table(shape):
    """Return the table grid (list of rows of cell strings) if this
    shape is a table; None otherwise."""
    if not getattr(shape, "has_table", False):
        return None
    table = shape.table
    return [
        [(cell.text or "").strip() for cell in row.cells]
        for row in table.rows
    ]


def _is_picture(shape) -> bool:
    """A picture shape includes inline images, but not vector
    shapes / freeforms / connectors which we count separately."""
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        return True
    # Some inserted images come through as LINKED_PICTURE.
    if shape.shape_type == MSO_SHAPE_TYPE.LINKED_PICTURE:
        return True
    return False


def _walk_shapes(shapes):
    """Yield every shape on a slide, descending into groups."""
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _walk_shapes(shape.shapes)
        else:
            yield shape


def parse_pptx_native(pptx_bytes: bytes) -> SlideMetadata:
    """Open a .pptx byte string and return per-slide metadata.

    Pure Python — no LibreOffice, no subprocess, no network. Safe to
    run inside the API task. Raises on corrupt/password-protected
    decks via python-pptx's own exception types; the caller wraps
    with a domain error in the upload route.
    """
    pres = Presentation(io.BytesIO(pptx_bytes))
    slides: list[SlideNative] = []

    # Skip hidden slides ("skip when presenting" in PowerPoint).
    # LibreOffice's PDF export drops these by default, so counting
    # them here would make slide_count != PDF page_count and trip
    # the upload-route alignment guard. The author's intent is
    # "this isn't part of the design" — respect it.
    visible_slides = [
        s for s in pres.slides if s.element.get("show") != "0"
    ]
    for idx, slide in enumerate(visible_slides, start=1):
        text_parts: list[str] = []
        tables: list[list[list[str]]] = []
        shape_count = 0
        image_count = 0

        for shape in _walk_shapes(slide.shapes):
            shape_count += 1
            if _is_picture(shape):
                image_count += 1
                continue
            tbl = _shape_table(shape)
            if tbl is not None:
                tables.append(tbl)
                # Tables also contribute their cell text to the
                # slide's native_text so the LLM sees the wording
                # without needing to walk a separate field.
                text_parts.append(
                    "\n".join("\t".join(row) for row in tbl)
                )
                continue
            txt = _shape_text(shape)
            if txt.strip():
                text_parts.append(txt)

        native = SlideNative(
            slide_index=idx,
            native_text="\n\n".join(p.strip() for p in text_parts if p.strip()),
            tables=tables,
            shape_count=shape_count,
            image_count=image_count,
            table_count=len(tables),
        )
        native.slide_type = classify_slide(native)
        slides.append(native)

    return SlideMetadata(slides=slides)


def classify_slide(slide: SlideNative) -> SlideType:
    """Heuristic classifier using the module-level thresholds.

    The catch-all is HYBRID. Every slide ends up in some bucket —
    misclassifications only affect prompt template selection, never
    correctness, because both text and image go to the model
    regardless.
    """
    text_len = len(slide.native_text)

    if text_len <= TITLE_TEXT_MAX and slide.shape_count <= TITLE_SHAPE_MAX:
        return SlideType.TITLE_ONLY
    if (
        text_len >= TEXT_HEAVY_TEXT_MIN
        and slide.shape_count <= TEXT_HEAVY_SHAPE_MAX
        and slide.image_count == 0
    ):
        return SlideType.TEXT_HEAVY
    if (
        text_len < DIAGRAM_HEAVY_TEXT_MAX
        and slide.shape_count >= DIAGRAM_HEAVY_SHAPE_MIN
    ):
        return SlideType.DIAGRAM_HEAVY
    return SlideType.HYBRID


# ---------------------------------------------------------------------------
# Sidecar client
# ---------------------------------------------------------------------------


class ConversionError(Exception):
    """Sidecar rejected the input (corrupt / password-protected)."""


class ConversionTimeout(Exception):
    """Sidecar's LibreOffice subprocess exceeded its 60s budget."""


class ConversionUpstreamError(Exception):
    """Sidecar couldn't fetch the source URL or push to the target."""


def convert_pptx_to_pdf(
    source_url: str,
    target_url: str,
    max_bytes: int = 100 * 1024 * 1024,
    base_url: Optional[str] = None,
) -> int:
    """Hand the sidecar two presigned URLs and wait for it to convert.

    Args:
      source_url: presigned GET URL on the uploaded .pptx in S3
      target_url: presigned PUT URL where the converted .pdf goes
      max_bytes: refuses oversized inputs (default 100MB)
      base_url: override of PPTX_CONVERTER_URL (for tests)

    Returns the converted PDF's page count (the sidecar reports it
    so the caller can sanity-check against slide_count).

    Raises ConversionError / ConversionTimeout / ConversionUpstreamError
    on the matching sidecar response codes. Network errors and
    everything else bubble up as httpx exceptions.
    """
    url = (base_url or os.getenv("PPTX_CONVERTER_URL", DEFAULT_CONVERTER_URL))
    convert_endpoint = f"{url.rstrip('/')}/convert"
    body = {
        "source_url": source_url,
        "target_url": target_url,
        "max_bytes": max_bytes,
    }

    try:
        resp = httpx.post(
            convert_endpoint,
            json=body,
            timeout=SIDECAR_TIMEOUT_S,
        )
    except httpx.TimeoutException as exc:
        # The HTTP call itself timed out before the sidecar could
        # respond. The user-facing message says the same as a 504
        # because the practical effect is identical.
        raise ConversionTimeout(
            f"pptx-converter sidecar did not respond within "
            f"{SIDECAR_TIMEOUT_S}s"
        ) from exc

    if 200 <= resp.status_code < 300:
        try:
            data = resp.json()
        except ValueError as exc:
            raise ConversionError(
                "pptx-converter returned non-JSON 2xx response"
            ) from exc
        return int(data.get("page_count", 0))
    if resp.status_code == 504:
        raise ConversionTimeout(
            "LibreOffice subprocess exceeded 60s; deck likely malformed"
        )
    if resp.status_code in (413, 422):
        # 413: oversized; 422: corrupt or password-protected
        detail = _safe_detail(resp)
        raise ConversionError(detail or f"pptx-converter rejected input ({resp.status_code})")
    if resp.status_code == 502:
        detail = _safe_detail(resp)
        raise ConversionUpstreamError(detail or "S3 upstream failed")
    # Anything else is unexpected — bubble up so the route handler
    # can log and 500.
    resp.raise_for_status()
    return 0  # unreachable, keeps mypy happy


def _safe_detail(resp: httpx.Response) -> str:
    """Best-effort extraction of an error detail string from the
    sidecar's response. Returns "" if the body isn't JSON or doesn't
    have a 'detail' field."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        return str(body.get("detail", ""))
    return ""


def rasterise_slides_to_s3(
    pdf_bytes: bytes,
    document_id: str,
    doc_store,
    dpi: int = 144,
) -> list[str]:
    """Rasterise each PDF page to a PNG and upload to S3.

    The sidecar produces a one-slide-per-page PDF; we
    walk the pages here and emit PNGs to documents/{id}/slides/{N}.png.
    144 DPI (2× the 72 DPI default) gives vision-quality output
    without ballooning file size.

    Returns the list of S3 keys, one per slide in order. The keys
    line up with SlideStatus.image_s3_key set during the upload
    route (slide_index is 1-based; key has the same number).
    """
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    keys: list[str] = []
    try:
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for page_index in range(doc.page_count):
            slide_number = page_index + 1
            pix = doc.load_page(page_index).get_pixmap(matrix=matrix, alpha=False)
            png_bytes = pix.tobytes("png")
            key = f"documents/{document_id}/slides/{slide_number}.png"
            doc_store.upload_document(
                png_bytes,
                key,
                content_type="image/png",
            )
            keys.append(key)
    finally:
        doc.close()
    return keys
