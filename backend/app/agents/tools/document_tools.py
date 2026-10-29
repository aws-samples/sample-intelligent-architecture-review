# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document retrieval tools for Strands agents."""

import os


from strands.tools import tool
from app.bedrock_models import SONNET, model_for
from app.services.bedrock_guardrail import wrap as _wrap_guardrail


@tool
def get_document_text(document_id: str) -> str:
    """Retrieve the full text content of a parsed document organized by sections with page numbers.
    If very little text was extracted (image-heavy/scanned PDF), it will note this so you can use analyze_page_images instead."""
    from app.services.validation_store import validation_store

    doc = validation_store.get_document(document_id)
    if not doc or not doc.sections:
        return "Document not found or not yet parsed. Try analyze_page_images if this is a scanned/image PDF."

    parts = []
    total_text_len = 0
    image_pages = []
    for section in doc.sections:
        parts.append(f"[Page {section.page_number}] {section.title}\n{section.content}")
        total_text_len += len(section.content)
        if hasattr(section, "images") and section.images:
            image_pages.append(section.page_number)

    result = "\n\n".join(parts)

    if total_text_len < 500 and image_pages:
        result += (
            f"\n\n⚠️ WARNING: Very little text extracted ({total_text_len} chars). "
            f"This appears to be a scanned/image-heavy document. "
            f"Use analyze_page_images to extract content from image pages: {image_pages}"
        )
    elif image_pages:
        result += f"\n\n📎 Note: {len(image_pages)} pages contain images. Use analyze_page_images for visual content on pages: {image_pages}"

    return result


@tool
def get_document_diagrams_info(document_id: str) -> str:
    """Get information about architecture diagrams extracted from the document."""
    from app.services.validation_store import validation_store

    doc = validation_store.get_document(document_id)
    if not doc or not doc.sections:
        return "No diagrams available."
    diagrams = []
    for section in doc.sections:
        if hasattr(section, "images") and section.images:
            for i, _ in enumerate(section.images):
                diagrams.append(
                    f"- Page {section.page_number}: {section.title} (Diagram {i+1})"
                )
    if not diagrams:
        return "No architecture diagrams detected in this document."
    return f"Found {len(diagrams)} diagrams:\n" + "\n".join(diagrams)


@tool
def get_page_content(document_id: str, page_number: int) -> str:
    """Get content from a specific page of the document."""
    from app.services.validation_store import validation_store

    doc = validation_store.get_document(document_id)
    if not doc or not doc.sections:
        return "Document not found."
    for section in doc.sections:
        if section.page_number == page_number:
            return f"[Page {page_number}] {section.title}\n{section.content}"
    return f"No content found for page {page_number}."


@tool
def analyze_page_images(document_id: str, page_numbers: str = "all") -> str:
    """Analyze images from document pages using Claude Vision. Use this for scanned/image-heavy PDFs.
    page_numbers: comma-separated page numbers like '1,2,3' or 'all' for all image pages."""
    import base64
    import boto3
    from app.services.validation_store import validation_store

    doc = validation_store.get_document(document_id)
    if not doc or not doc.sections:
        return "Document not found or not parsed."

    # Collect images from specified pages
    images_to_analyze = []
    for section in doc.sections:
        if not hasattr(section, "images") or not section.images:
            continue
        if page_numbers != "all":
            target_pages = [int(p.strip()) for p in page_numbers.split(",")]
            if section.page_number not in target_pages:
                continue
        for img_b64 in section.images[:3]:  # Max 3 images per page
            images_to_analyze.append({
                "page": section.page_number,
                "title": section.title,
                "image": img_b64,
            })

    if not images_to_analyze:
        return "No images found on the specified pages."

    # Use Claude Vision to analyze images (batch up to 5)
    client = _wrap_guardrail(
        boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
    )
    results = []

    for img_info in images_to_analyze[:5]:
        content = [
            {"text": f"Extract ALL text and requirements from this page image (Page {img_info['page']}: {img_info['title']}). "
                     "Focus on HA/availability requirements, numbered rules, MUST/SHALL statements, and any policy criteria."},
            {"image": {"format": "png", "source": {"bytes": base64.b64decode(img_info["image"])}}},
        ]
        try:
            response = client.converse(
                modelId=model_for("BEDROCK_MODEL_ID", SONNET),
                messages=[{"role": "user", "content": content}],
                inferenceConfig={"maxTokens": 4000, "temperature": 0.1},
            )
            text = response["output"]["message"]["content"][0]["text"]
            results.append(f"[Page {img_info['page']}] {img_info['title']}:\n{text}")
        except Exception as e:
            results.append(f"[Page {img_info['page']}] Error analyzing image: {str(e)}")

    return "\n\n---\n\n".join(results)
