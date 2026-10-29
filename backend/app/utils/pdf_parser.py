# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PDF parsing utilities using PyMuPDF (fitz)."""

import base64
import logging
import re
from dataclasses import dataclass, field

import fitz  # pymupdf

logger = logging.getLogger(__name__)


def _table_to_markdown(rows: list[list]) -> str:
    """Render a 2D table as a markdown table.

    Bedrock parses markdown tables natively, so the
    extraction agent can read tier × layer matrices without further
    pre-processing. Empty cells are preserved as blank cells (matrices
    sometimes have intentional gaps).

    Cells are stringified with internal newlines folded to spaces so
    each markdown row stays on a single line.
    """
    if not rows:
        return ""

    def _cell(v) -> str:
        s = "" if v is None else str(v)
        return s.replace("\n", " ").replace("|", "\\|").strip()

    width = max(len(r) for r in rows)
    norm = [[_cell(c) for c in r] + [""] * (width - len(r)) for r in rows]

    header = norm[0]
    body = norm[1:] if len(norm) > 1 else []

    if all(not c for c in header):
        header = [f"Col {i + 1}" for i in range(width)]
        body = norm

    out = ["| " + " | ".join(header) + " |"]
    out.append("| " + " | ".join(["---"] * width) + " |")
    for r in body:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def extract_page_tables(page) -> list[str]:
    """Return markdown-rendered tables found on this page.

    Uses PyMuPDF's find_tables() (1.23+). Returns an
    empty list if no tables detected or if extraction throws.
    """
    try:
        finder = page.find_tables()
        tables = list(finder.tables)
    except Exception as e:
        logger.warning(
            f"find_tables failed on page {page.number + 1}: {e}"
        )
        return []

    out: list[str] = []
    for tbl in tables:
        try:
            rows = tbl.extract()
        except Exception as e:
            logger.warning(
                f"Table.extract failed on page {page.number + 1}: {e}"
            )
            continue
        md = _table_to_markdown(rows)
        if md:
            out.append(md)
    return out


@dataclass
class RawSection:
    """Raw section extracted from PDF."""
    title: str
    content: str
    page_number: int
    level: int = 1
    images: list[str] = field(default_factory=list)


@dataclass
class PDFParseResult:
    """Complete result of PDF parsing."""
    sections: list[RawSection]
    full_text: str
    page_count: int
    images: list[str]
    metadata: dict


def extract_text_and_sections(pdf_bytes: bytes) -> PDFParseResult:
    """
    Extract text, sections, and images from a PDF file.

    Args:
        pdf_bytes: Raw PDF file bytes

    Returns:
        PDFParseResult with extracted content
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_count = len(doc)

    all_text_parts: list[str] = []
    all_sections: list[RawSection] = []
    all_images: list[str] = []

    current_section_title = "Introduction"
    current_section_content: list[str] = []
    current_section_page = 1
    current_section_level = 1
    current_section_images: list[str] = []

    for page_num in range(page_count):
        page = doc[page_num]
        page_number = page_num + 1

        # Extract text with formatting info
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]

        for block in blocks:
            if block["type"] == 0:  # Text block
                for line in block["lines"]:
                    line_text = ""
                    is_heading = False
                    max_font_size = 0

                    for span in line["spans"]:
                        line_text += span["text"]
                        font_size = span["size"]
                        max_font_size = max(max_font_size, font_size)

                        # Detect headings: larger font, bold, or specific patterns
                        is_bold = "bold" in span["font"].lower() or "Bold" in span["font"]
                        if font_size >= 14 or (is_bold and font_size >= 12):
                            is_heading = True

                    line_text = line_text.strip()
                    if not line_text:
                        continue

                    # Also detect numbered headings like "1.", "1.1", "2.3.1"
                    heading_pattern = re.match(
                        r"^(\d+\.[\d.]*)\s+(.+)$", line_text
                    )

                    if is_heading or heading_pattern:
                        # Save previous section
                        if current_section_content:
                            section_text = "\n".join(current_section_content).strip()
                            if section_text:
                                all_sections.append(
                                    RawSection(
                                        title=current_section_title,
                                        content=section_text,
                                        page_number=current_section_page,
                                        level=current_section_level,
                                        images=current_section_images,
                                    )
                                )

                        # Start new section
                        current_section_title = line_text
                        current_section_content = []
                        current_section_page = page_number
                        current_section_images = []

                        # Determine heading level
                        if heading_pattern:
                            dots = heading_pattern.group(1).count(".")
                            current_section_level = min(dots + 1, 4)
                        elif max_font_size >= 18:
                            current_section_level = 1
                        elif max_font_size >= 14:
                            current_section_level = 2
                        else:
                            current_section_level = 3
                    else:
                        current_section_content.append(line_text)
                        all_text_parts.append(line_text)

            elif block["type"] == 1:  # Image block
                # Extract image
                try:
                    image_bytes = block.get("image")
                    if image_bytes:
                        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
                        all_images.append(image_b64)
                        current_section_images.append(image_b64)
                except Exception as e:
                    logger.warning(f"Failed to extract image on page {page_number}: {e}")

        # Also extract images via page.get_images()
        image_list = page.get_images(full=True)
        for img_index, img_info in enumerate(image_list):
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                if base_image:
                    image_bytes = base_image["image"]
                    # Convert to PNG if not already
                    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
                    if image_b64 not in all_images:
                        all_images.append(image_b64)
                        current_section_images.append(image_b64)
            except Exception as e:
                logger.warning(
                    f"Failed to extract image {img_index} from page {page_number}: {e}"
                )

    # Save last section
    if current_section_content:
        section_text = "\n".join(current_section_content).strip()
        if section_text:
            all_sections.append(
                RawSection(
                    title=current_section_title,
                    content=section_text,
                    page_number=current_section_page,
                    level=current_section_level,
                    images=current_section_images,
                )
            )

    # If no sections were found, create a single section with all text
    if not all_sections and all_text_parts:
        all_sections.append(
            RawSection(
                title="Document Content",
                content="\n".join(all_text_parts),
                page_number=1,
                level=1,
                images=all_images,
            )
        )

    # Table-recovery pass.
    #
    # The heading-driven loop above is blind to table-only pages: a
    # tier × layer matrix with no large/bold headings produces no
    # Section, and the table cells get either dropped or mashed into
    # the previous section's content as denatured tokens. The fix is
    # additive: walk every page, extract any detected tables as
    # markdown, and either append to that page's existing section or
    # synthesize a "Page N" section so the table content has somewhere
    # to live. Heading-based parsing is unchanged.
    for page_num in range(page_count):
        page = doc[page_num]
        tables = extract_page_tables(page)
        if not tables:
            continue
        page_number = page_num + 1
        block = "\n\n".join(tables)

        existing = next(
            (s for s in all_sections if s.page_number == page_number),
            None,
        )
        if existing is not None:
            existing.content = (
                existing.content
                + "\n\n## Tables on this page\n\n"
                + block
            ).strip()
        else:
            all_sections.append(
                RawSection(
                    title=f"Page {page_number}",
                    content=block,
                    page_number=page_number,
                    level=1,
                )
            )
            all_text_parts.append(block)

    # Keep section order stable on (page, then original insertion order).
    # Python's sort is stable, so synthetic Page N sections that match
    # an already-present page slot stay after the heading-driven ones.
    all_sections.sort(key=lambda s: s.page_number)

    full_text = "\n".join(all_text_parts)

    metadata = {
        "title": doc.metadata.get("title", ""),
        "author": doc.metadata.get("author", ""),
        "subject": doc.metadata.get("subject", ""),
        "creator": doc.metadata.get("creator", ""),
        "page_count": page_count,
        "image_count": len(all_images),
        "section_count": len(all_sections),
    }

    doc.close()

    return PDFParseResult(
        sections=all_sections,
        full_text=full_text,
        page_count=page_count,
        images=all_images,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Multimodal PDF support
#
# Renders each page to a PNG so the multimodal validator can hand the
# whole page (text + diagram) to Bedrock Converse. Detection helper
# lets the dispatcher avoid the multimodal cost on PDFs that have no
# embedded images at all (rare in some SDDs but common for
# Confluence / Word exports).
# ---------------------------------------------------------------------------


def render_pages_to_png(pdf_bytes: bytes, dpi: int = 150) -> list[bytes]:
    """Render every page of *pdf_bytes* to a PNG byte string.

    150 DPI strikes a balance between legibility (Bedrock multimodal
    Sonnet downscales to ~1568 px on the long edge anyway) and file
    size. Returns an empty list for an empty PDF rather than raising.
    """
    if not pdf_bytes:
        return []
    out: list[bytes] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out.append(pix.tobytes("png"))
    finally:
        doc.close()
    return out


def has_diagram_pages(pdf_bytes: bytes) -> bool:
    """True if any page of *pdf_bytes* contains at least one embedded
    raster image. Used by the validator dispatcher: PDFs that are pure
    text (no diagrams) keep the cheaper text-only path."""
    if not pdf_bytes:
        return False
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            if page.get_images(full=False):
                return True
    finally:
        doc.close()
    return False
