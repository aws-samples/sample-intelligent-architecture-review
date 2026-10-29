# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Teams integration routes - Webhooks and notifications."""

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.dependencies import get_validation_store
from app.api.rbac import Role, require_role
from app.config import settings
from app.services.validation_store import ValidationStore

logger = logging.getLogger(__name__)

router = APIRouter()


class TeamsNotification(BaseModel):
    """Request to send a Teams notification."""
    title: str = Field(description="Notification title")
    message: str = Field(description="Notification message body")
    validation_id: Optional[str] = Field(default=None, description="Related validation ID")
    severity: str = Field(default="info", description="Severity: info, warning, error, success")


class TeamsWebhookPayload(BaseModel):
    """Incoming webhook payload from Teams."""
    type: Optional[str] = None
    text: Optional[str] = None
    from_name: Optional[str] = Field(default=None, alias="from")
    channel_id: Optional[str] = None
    conversation_id: Optional[str] = None


@router.post("/webhook")
async def teams_webhook(
    request: Request,
    store: ValidationStore = Depends(get_validation_store),
):
    """
    Receive incoming webhooks from Microsoft Teams.

    Handles messages and commands from Teams channels.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info(f"Received Teams webhook: {body.get('type', 'unknown')}")

    # Extract message text
    text = body.get("text", "").strip()

    if not text:
        return {"status": "ok", "message": "No text content received"}

    # Simple command handling
    response_text = _handle_teams_command(text, store)

    return {
        "type": "message",
        "text": response_text,
    }


@router.post("/notify")
async def send_teams_notification(
    notification: TeamsNotification,
    # Sends caller-supplied text into the organisation's Teams channel using
    # the server's webhook, so it must not be open to every authenticated user.
    claims: dict = Depends(require_role(Role.VALIDATOR)),
):
    """
    Send a notification to a Teams channel via webhook.

    Requires TEAMS_WEBHOOK_URL to be configured.
    """
    if not settings.teams_webhook_url:
        raise HTTPException(
            status_code=503,
            detail="Teams webhook URL not configured. Set TEAMS_WEBHOOK_URL environment variable.",
        )

    # Build Teams Adaptive Card
    color = {
        "info": "0078D4",
        "warning": "FFA500",
        "error": "FF0000",
        "success": "00AA00",
    }.get(notification.severity, "0078D4")

    card_payload = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": color,
        "summary": notification.title,
        "sections": [
            {
                "activityTitle": notification.title,
                "facts": [],
                "markdown": True,
                "text": notification.message,
            }
        ],
    }

    if notification.validation_id:
        card_payload["sections"][0]["facts"].append(
            {"name": "Validation ID", "value": notification.validation_id}
        )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.teams_webhook_url,
                json=card_payload,
                timeout=10.0,
            )
            response.raise_for_status()

        logger.info(f"Teams notification sent: {notification.title}")
        return {"status": "sent", "title": notification.title}

    except httpx.HTTPStatusError as e:
        logger.error(f"Teams webhook returned error: {e.response.status_code}")
        raise HTTPException(
            status_code=502,
            detail=f"Teams webhook returned error: {e.response.status_code}",
        )
    except httpx.RequestError as e:
        logger.error(f"Failed to send Teams notification: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to Teams webhook: {str(e)}",
        )


def _handle_teams_command(text: str, store: ValidationStore) -> str:
    """Handle a command from Teams and return response text."""
    text_lower = text.lower().strip()

    # Remove bot mention if present
    if "<at>" in text_lower:
        # Strip mention tags
        import re
        text_lower = re.sub(r"<at>.*?</at>", "", text_lower).strip()

    if text_lower in ("help", "/help"):
        return (
            "**HA Validation Bot Commands:**\n\n"
            "- `status` - Show system status\n"
            "- `documents` - List uploaded documents\n"
            "- `validations` - List recent validations\n"
            "- `help` - Show this help message\n"
        )

    elif text_lower in ("status", "/status"):
        docs = store.list_documents()
        vals = store.list_validations()
        return (
            f"**System Status: Healthy**\n\n"
            f"- Documents: {len(docs)}\n"
            f"- Validations: {len(vals)}\n"
        )

    elif text_lower in ("documents", "/documents", "docs"):
        docs = store.list_documents()
        if not docs:
            return "No documents uploaded yet."
        lines = ["**Recent Documents:**\n"]
        for doc in docs[:5]:
            lines.append(f"- `{doc.document_id}`: {doc.filename} ({doc.status.value})")
        return "\n".join(lines)

    elif text_lower in ("validations", "/validations", "vals"):
        vals = store.list_validations()
        if not vals:
            return "No validations completed yet."
        lines = ["**Recent Validations:**\n"]
        for val in vals[:5]:
            # Distinguish a real 0.0 (honest FAIL) from None (N/A, no
            # applicable rules) from an in-progress run. A falsy check
            # would render all three as "Pending".
            if val.overall_score is not None:
                score_str = f"Score: {val.overall_score}"
            else:
                score_str = "N/A"
            lines.append(
                f"- `{val.validation_id}`: {val.status.value} ({score_str})"
            )
        return "\n".join(lines)

    else:
        return (
            f"I didn't understand: *{text}*\n\n"
            "Type `help` for available commands."
        )
