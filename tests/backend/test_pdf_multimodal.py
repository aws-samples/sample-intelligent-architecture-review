# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for diagram-aware PDF validation.

Drives the multimodal PDF path: render each page to PNG, send the
page text + image to Bedrock per (page × rule_batch). Mirrors the
PPTX validator's structure but per-page instead of per-slide.
"""

from __future__ import annotations

import io

import fitz
import pytest


# ---------------------------------------------------------------------------
# Step 4 — page renderer
# ---------------------------------------------------------------------------


def _make_pdf_with_pages(num_pages: int) -> bytes:
    """Build a real fitz-generated PDF in-memory so the test does not
    depend on any fixture file. Each page gets a page-number label so
    we can assert the renderer captures distinct visual content."""
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page(width=300, height=200)
        page.insert_text((40, 100), f"Page {i + 1}")
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    return out.getvalue()


def test_render_pages_to_png_returns_one_png_per_page():
    """render_pages_to_png must return exactly N PNG byte strings for
    an N-page PDF, in order, each starting with the PNG magic bytes."""
    from app.utils.pdf_parser import render_pages_to_png

    pdf_bytes = _make_pdf_with_pages(3)
    pages = render_pages_to_png(pdf_bytes, dpi=72)

    assert len(pages) == 3
    for png in pages:
        assert isinstance(png, bytes)
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "missing PNG magic header"
        # 72 DPI on a 300x200 page is small but non-empty.
        assert len(png) > 200


def test_render_pages_to_png_handles_empty_input():
    """Empty/missing PDF bytes must yield empty list, not crash —
    PyMuPDF refuses to save a zero-page document, so the equivalent
    'no input' condition we care about is empty bytes."""
    from app.utils.pdf_parser import render_pages_to_png

    assert render_pages_to_png(b"") == []


# ---------------------------------------------------------------------------
# Step 5 — diagram detection
# ---------------------------------------------------------------------------


def test_has_diagram_pages_false_when_pure_text():
    """A PDF with only text and no embedded images should be flagged
    as not-diagram-bearing so the dispatcher keeps the cheap text path."""
    from app.utils.pdf_parser import has_diagram_pages

    pdf_bytes = _make_pdf_with_pages(2)
    assert has_diagram_pages(pdf_bytes) is False


def test_has_diagram_pages_true_when_image_embedded():
    """A PDF with at least one page containing an embedded image
    must trigger the multimodal path. Build the embedded PNG via
    PyMuPDF itself so we don't depend on a hand-rolled bytestream."""
    from app.utils.pdf_parser import has_diagram_pages

    # Synthesize a valid PNG by rendering a tiny 4x4 fitz page first.
    src = fitz.open()
    src.new_page(width=4, height=4)
    pix = src[0].get_pixmap()
    real_png = pix.tobytes("png")
    src.close()

    doc = fitz.open()
    text_page = doc.new_page(width=300, height=200)
    text_page.insert_text((40, 100), "text only")
    image_page = doc.new_page(width=300, height=200)
    image_page.insert_image(fitz.Rect(20, 20, 80, 80), stream=real_png)
    out = io.BytesIO()
    doc.save(out)
    doc.close()

    assert has_diagram_pages(out.getvalue()) is True


# ---------------------------------------------------------------------------
# Step 6 — multimodal page message builder
# ---------------------------------------------------------------------------


def test_build_page_message_carries_text_and_image():
    """The message we hand to Bedrock Converse must contain both a
    text block (rules + page text) and an image block (the page PNG).
    Bedrock's multimodal Converse API requires images to be raw bytes
    inside `{"image": {"format": "png", "source": {"bytes": ...}}}`."""
    from app.services.pdf_multimodal import _build_page_message

    rules = [
        {
            "id": "HA-001",
            "name": "Multi-AZ",
            "description": "DB must be Multi-AZ",
            "severity": "critical",
            "validation_criteria": "Look for Multi-AZ in the diagram",
        }
    ]
    page_text = "Page 12: Aurora cluster in us-east-1 with cross-region DR"
    page_png = b"\x89PNG\r\n\x1a\nfake-png-bytes"

    msg = _build_page_message(
        page_number=12,
        page_text=page_text,
        page_png=page_png,
        rule_batch=rules,
    )

    # Bedrock Converse expects role + content blocks.
    assert msg["role"] == "user"
    blocks = msg["content"]
    text_blocks = [b for b in blocks if "text" in b]
    image_blocks = [b for b in blocks if "image" in b]

    assert len(text_blocks) >= 1
    assert any("HA-001" in b["text"] for b in text_blocks), \
        "rule id must appear in the prompt so the model knows what to evaluate"
    assert any("Page 12" in b["text"] or "page 12" in b["text"].lower()
               for b in text_blocks), \
        "page number must appear so cited evidence stays grounded"
    assert any(page_text in b["text"] for b in text_blocks), \
        "page text must be embedded in the prompt"

    assert len(image_blocks) == 1
    img = image_blocks[0]["image"]
    assert img["format"] == "png"
    assert img["source"]["bytes"] == page_png, \
        "image bytes must be passed RAW (boto3 base64-encodes for us)"


# ---------------------------------------------------------------------------
# Step 7 — multimodal validator (Bedrock mocked)
# ---------------------------------------------------------------------------


def test_validate_pdf_multimodal_produces_findings_with_trace_and_image_ref(monkeypatch):
    """End-to-end of validate_pdf_multimodal_document with a mocked
    Bedrock client. Asserts:

      - one finding per rule
      - each finding has a populated trace (chunk_excerpt = page text,
        prompt + raw_response set)
      - findings cite the page number we put in the canned response
    """
    from unittest.mock import MagicMock

    from app.services.pdf_multimodal import validate_pdf_multimodal_document

    # Two-page PDF; page 2 carries the architecture diagram.
    pdf_bytes = _make_pdf_with_pages(2)
    rules = [
        {
            "id": "HA-001",
            "name": "Multi-AZ",
            "description": "DB must be Multi-AZ",
            "severity": "critical",
            "validation_criteria": "Look for Multi-AZ in the diagram",
            "hosting_scope": "on_prem",
            "applies_to_tiers": [1],
            "layer": "data",
        },
    ]
    page_texts = ["page one text", "page two has the architecture"]

    # Canned Bedrock response: one rule per call, fail with diagram-cited evidence.
    canned_response = {
        "output": {
            "message": {
                "content": [
                    {
                        "text": (
                            '[{"rule_id": "HA-001", "verdict": "fail", '
                            '"evidence": "the diagram on page 2 shows a '
                            'single-AZ topology", '
                            '"recommendation": "deploy to multiple AZs", '
                            '"affected_components": ["RDS"]}]'
                        )
                    }
                ]
            }
        },
        "usage": {"inputTokens": 100, "outputTokens": 50},
    }
    fake_client = MagicMock()
    fake_client.converse.return_value = canned_response

    monkeypatch.setattr(
        "app.services.pdf_multimodal._make_bedrock_client", lambda: fake_client
    )

    result = validate_pdf_multimodal_document(
        pdf_bytes=pdf_bytes,
        page_texts=page_texts,
        document_id="doc-test",
        standards=rules,
        declared_tier=1,
        declared_scope="on_prem",
    )

    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.requirement_id == "HA-001"
    assert "diagram" in f.evidence.lower()
    assert f.trace is not None, "multimodal findings must carry a trace"
    assert f.trace.get("chunk_excerpt"), "trace.chunk_excerpt must be populated"
    assert f.trace.get("prompt"), "trace.prompt must be populated"
    assert f.trace.get("raw_response"), "trace.raw_response must be populated"
    # The trace's chunk_excerpt is doc-level (whole-doc multimodal):
    # `[Page N] <text>` markers concatenated. Verify each page's
    # text appears so the audit trail is complete.
    excerpt = f.trace["chunk_excerpt"]
    for pt in page_texts:
        assert pt in excerpt, f"page text {pt!r} missing from trace excerpt"


# ---------------------------------------------------------------------------
# Whole-doc multimodal — option A from spec
# ---------------------------------------------------------------------------


def test_build_whole_doc_message_includes_all_text_and_capped_images():
    """The whole-doc builder concatenates every page's text and
    embeds up to MAX_IMAGES_PER_CALL pages as images. Pages beyond
    the cap go in as text only — the model still reads them, just
    without their visuals."""
    from app.services.pdf_multimodal import (
        _build_whole_doc_message,
        MAX_IMAGES_PER_CALL,
    )

    page_texts = [f"page {i} text" for i in range(1, 25)]  # 24 pages
    page_pngs = [f"png{i}".encode() for i in range(1, 25)]
    rules = [
        {"id": "HA-001", "name": "Multi-AZ", "description": "x",
         "severity": "critical", "validation_criteria": "y"}
    ]

    msg = _build_whole_doc_message(
        page_texts=page_texts,
        page_pngs=page_pngs,
        diagram_page_indices=list(range(24)),  # all pages have diagrams
        rule_batch=rules,
    )

    assert msg["role"] == "user"
    text_blocks = [b for b in msg["content"] if "text" in b]
    image_blocks = [b for b in msg["content"] if "image" in b]

    # Every page's text must show up somewhere in the prompt.
    full_text = " ".join(b["text"] for b in text_blocks)
    for i in [1, 12, 24]:
        assert f"page {i} text" in full_text, f"page {i} text missing"

    # Image cap is enforced.
    assert len(image_blocks) == MAX_IMAGES_PER_CALL
    assert MAX_IMAGES_PER_CALL == 20  # Bedrock Converse limit

    # Rule batch is in the prompt.
    assert any("HA-001" in b["text"] for b in text_blocks)


def test_build_whole_doc_message_handles_no_diagrams():
    """Pure-text PDFs (or PPTX where diagrams already live in slide
    text) yield an image-free message. Validator still works text-only."""
    from app.services.pdf_multimodal import _build_whole_doc_message

    msg = _build_whole_doc_message(
        page_texts=["intro", "body", "conclusion"],
        page_pngs=[],
        diagram_page_indices=[],
        rule_batch=[{"id": "HA-001", "name": "x", "description": "y",
                     "severity": "low", "validation_criteria": "z"}],
    )
    assert [b for b in msg["content"] if "image" in b] == []


def test_build_page_message_works_without_image():
    """Some pages may have no rendered image (e.g. text-only intro
    pages); the builder must still produce a valid Bedrock message
    with just the text block."""
    from app.services.pdf_multimodal import _build_page_message

    msg = _build_page_message(
        page_number=1,
        page_text="cover page",
        page_png=None,
        rule_batch=[{"id": "HA-001", "name": "x", "description": "y",
                     "severity": "low", "validation_criteria": "z"}],
    )
    image_blocks = [b for b in msg["content"] if "image" in b]
    assert image_blocks == []


# ---------------------------------------------------------------------------
# Extended thinking / reasoning capture
#
# Spike confirmed canonical Bedrock response shape (2026-05-27):
#   content = response["output"]["message"]["content"]  # list of blocks
#   - reasoning block:  {"reasoningContent": {"reasoningText": {"text": "...",
#                                                               "signature": "..."}}}
#   - answer block:     {"text": "..."}
# Both claude-sonnet-4-20250514-v1:0 AND claude-sonnet-4-6 support
# thinking, so chunked + PPTX paths do NOT need a model upgrade.
# ---------------------------------------------------------------------------


def _bedrock_response_with_reasoning(reasoning_text: str, answer_text: str,
                                     signature: str = "sig-abc") -> dict:
    """Build a Bedrock Converse response in the shape Bedrock returns."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "reasoningContent": {
                            "reasoningText": {
                                "text": reasoning_text,
                                "signature": signature,
                            }
                        }
                    },
                    {"text": answer_text},
                ],
            }
        },
        "usage": {"inputTokens": 100, "outputTokens": 200, "totalTokens": 300},
    }


def test_extract_reasoning_blocks_returns_text_and_signature():
    """Step 1 — parser helper. Given Bedrock content blocks (per the observed Bedrock
    canonical shape), returns (reasoning_text, signature). Used by all
    three validator paths."""
    from app.services.pdf_multimodal import _extract_reasoning_blocks

    resp = _bedrock_response_with_reasoning(
        "Let me think... 17 * 23 = 391",
        "**Answer:** 391",
        signature="opaque-sig-xyz",
    )
    blocks = resp["output"]["message"]["content"]

    reasoning_text, signature = _extract_reasoning_blocks(blocks)
    assert reasoning_text == "Let me think... 17 * 23 = 391"
    assert signature == "opaque-sig-xyz"


def test_extract_reasoning_blocks_handles_no_reasoning():
    """When the model didn't emit a reasoning block (thinking off, or
    short response), parser returns ("", None) cleanly — no crash."""
    from app.services.pdf_multimodal import _extract_reasoning_blocks

    blocks = [{"text": "Just an answer, no thinking."}]
    reasoning_text, signature = _extract_reasoning_blocks(blocks)
    assert reasoning_text == ""
    assert signature is None


def test_extract_reasoning_blocks_handles_multiple_reasoning_blocks():
    """Defensive: if Bedrock ever returns multiple reasoning blocks
    (unlikely but legal per the spec), concatenate the text and use
    the first signature."""
    from app.services.pdf_multimodal import _extract_reasoning_blocks

    blocks = [
        {"reasoningContent": {"reasoningText": {"text": "First thought.",
                                                "signature": "sig-1"}}},
        {"reasoningContent": {"reasoningText": {"text": " Second thought.",
                                                "signature": "sig-2"}}},
        {"text": "Final answer."},
    ]
    reasoning_text, signature = _extract_reasoning_blocks(blocks)
    assert reasoning_text == "First thought. Second thought."
    assert signature == "sig-1"


def test_validate_pdf_multimodal_persists_reasoning_in_trace(monkeypatch):
    """Step 2 — full async validator with mocked Bedrock that emits a
    reasoning block. The Finding.trace must carry reasoning, model_id,
    and reasoning_signature."""
    from unittest.mock import MagicMock

    from app.services.pdf_multimodal import validate_pdf_multimodal_document

    pdf_bytes = _make_pdf_with_pages(2)
    rules = [
        {
            "id": "HA-001",
            "name": "Multi-AZ",
            "description": "DB must be Multi-AZ",
            "severity": "critical",
            "validation_criteria": "Look for Multi-AZ",
            "hosting_scope": "on_prem",
            "applies_to_tiers": [1],
            "layer": "data",
        },
    ]
    page_texts = ["page one text", "page two has the architecture"]

    fake_client = MagicMock()
    fake_client.converse.return_value = _bedrock_response_with_reasoning(
        "Looking at page 2, I can see the architecture includes "
        "Multi-AZ. The diagram on page 2 confirms it. So this passes.",
        '[{"rule_id": "HA-001", "verdict": "pass", '
        '"evidence": "Page 2 shows Multi-AZ", "recommendation": ""}]',
        signature="opaque-sig-real",
    )

    monkeypatch.setattr(
        "app.services.pdf_multimodal._make_bedrock_client", lambda: fake_client
    )

    result = validate_pdf_multimodal_document(
        pdf_bytes=pdf_bytes,
        page_texts=page_texts,
        document_id="doc-test",
        standards=rules,
        declared_tier=1,
        declared_scope="on_prem",
    )

    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.trace is not None
    assert f.trace.get("reasoning"), "trace.reasoning must be populated"
    assert "page 2" in f.trace["reasoning"].lower()
    assert f.trace.get("model_id"), "trace.model_id must be populated"
    assert f.trace.get("reasoning_signature") == "opaque-sig-real"


def test_validate_pdf_multimodal_omits_thinking_when_disabled(monkeypatch):
    """Step 5 — when ENABLE_THINKING_MULTIMODAL is false, the Converse
    call must NOT include the additionalModelRequestFields key, and
    trace.reasoning ends up None."""
    from unittest.mock import MagicMock

    from app.services.pdf_multimodal import validate_pdf_multimodal_document

    monkeypatch.setenv("ENABLE_THINKING_MULTIMODAL", "false")

    pdf_bytes = _make_pdf_with_pages(1)
    rules = [{
        "id": "HA-001", "name": "x", "description": "y",
        "severity": "low", "validation_criteria": "z",
        "hosting_scope": "on_prem", "applies_to_tiers": [1], "layer": "data",
    }]

    fake_client = MagicMock()
    # Response without reasoning, since thinking is off:
    fake_client.converse.return_value = {
        "output": {"message": {"content": [
            {"text": '[{"rule_id":"HA-001","verdict":"pass","evidence":"e","recommendation":""}]'}
        ]}},
        "usage": {"inputTokens": 10, "outputTokens": 20},
    }
    monkeypatch.setattr(
        "app.services.pdf_multimodal._make_bedrock_client", lambda: fake_client
    )

    result = validate_pdf_multimodal_document(
        pdf_bytes=pdf_bytes,
        page_texts=["only page"],
        document_id="doc-test",
        standards=rules,
        declared_tier=1,
        declared_scope="on_prem",
    )

    # Verify Converse was called WITHOUT additionalModelRequestFields
    call_args = fake_client.converse.call_args
    assert "additionalModelRequestFields" not in call_args.kwargs, \
        "thinking should be disabled when ENABLE_THINKING_MULTIMODAL=false"
    # And reasoning ends up None
    f = result.findings[0]
    assert f.trace.get("reasoning") is None


# ---------------------------------------------------------------------------
# Cancellation — the multimodal path must honour a cancel flag and raise
# ValidationCancelled (post-loop check), NOT return a completed result. This
# is the highest-value gap the review flagged; a small single-batch doc can't
# be interrupted mid-Bedrock-call, so the post-loop check must discard it.
# ---------------------------------------------------------------------------


def test_multimodal_cancellation_raises_not_completes(monkeypatch):
    import asyncio
    from unittest.mock import MagicMock

    from app.services.pdf_multimodal import _validate_pdf_multimodal_async
    from app.services.parallel_validator import (
        request_cancel,
        clear_cancel,
        ValidationCancelled,
    )

    pdf_bytes = _make_pdf_with_pages(1)
    rules = [{
        "id": "HA-001", "name": "Multi-AZ", "description": "d",
        "severity": "high", "validation_criteria": "c",
        "hosting_scope": "both", "applies_to_tiers": [1], "layer": "data",
    }]
    canned = {
        "output": {"message": {"content": [
            {"text": '[{"rule_id": "HA-001", "verdict": "pass", "evidence": "ok"}]'}
        ]}},
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }
    fake_client = MagicMock()
    fake_client.converse.return_value = canned
    monkeypatch.setattr(
        "app.services.pdf_multimodal._make_bedrock_client", lambda: fake_client
    )

    vid = "val-cancel-e2e"
    request_cancel(vid)  # flag set before the run — post-loop check must catch it
    try:
        with pytest.raises(ValidationCancelled):
            asyncio.run(
                _validate_pdf_multimodal_async(
                    pdf_bytes=pdf_bytes,
                    page_texts=["p1"],
                    document_id="doc-x",
                    standards=rules,
                    declared_tier=1,
                    declared_scope="both",
                    validation_id=vid,
                )
            )
    finally:
        clear_cancel(vid)
