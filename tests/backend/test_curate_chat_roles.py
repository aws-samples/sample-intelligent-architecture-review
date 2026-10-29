# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Curate-with-AI must be completable by the role that starts it.

Two endpoints have confusingly similar names but belong to different features:

  POST /api/standards/curate  — picks the most relevant EXISTING rules for one
      document. Read-only rule selection, reached from the Run Validation
      dialog. Allowed for validator AND curator.
  POST /api/standards/chat    — the AI rule curator that proposes library
      changes. Curator only.

/curate used to allow validator alone, which locked a curator out of AI
curation of their own library. And the Run Validation dialog offered every
caller a "Save to library" button that POSTs /api/standards (curator-only), so
a validator's only path through the suggestion flow ended in a raw 403.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _client_as(role: str) -> TestClient:
    from app.main import app
    from app.api.auth import get_current_user
    from app.services.user_roles_store import user_roles_store

    user_roles_store._available = False
    user_roles_store._mem = {"s": {"sub": "s", "role": role, "email": "u@x.com"}}
    app.dependency_overrides[get_current_user] = lambda: {"sub": "s", "email": "u@x.com"}
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides.pop(get_current_user, None)


def _post_curate(client: TestClient):
    # A document id that does not exist: an authorised caller gets past the role
    # check and fails on lookup (404/400), an unauthorised one gets 403. That
    # distinction is what these tests assert, with no Bedrock call either way.
    return client.post("/api/standards/curate", json={"document_id": "nope", "top_n": 5})


class TestCurateEndpoint:
    @pytest.mark.parametrize("role", ["validator", "curator", "admin"])
    def test_writing_roles_may_curate(self, role):
        """Both roles that own part of this flow can start it."""
        resp = _post_curate(_client_as(role))
        assert resp.status_code != 403, (
            f"{role} was refused /standards/curate: {resp.text}"
        )

    def test_viewer_may_not_curate(self):
        """It spends model tokens and picks the rules a run is judged against."""
        assert _post_curate(_client_as("viewer")).status_code == 403


class TestChatEndpoint:
    """The AI rule curator proposes library changes — curator only, unchanged."""

    def _post_chat(self, client: TestClient):
        return client.post("/api/standards/chat", json={"message": "hi"})

    @pytest.mark.parametrize("role", ["curator", "admin"])
    def test_curator_roles_may_chat(self, role):
        assert self._post_chat(_client_as(role)).status_code != 403

    @pytest.mark.parametrize("role", ["validator", "viewer"])
    def test_non_curators_may_not_chat(self, role):
        assert self._post_chat(_client_as(role)).status_code == 403


class TestLibraryWriteStaysCuratorOnly:
    """The asymmetry is resolved by gating the UI, not by widening this."""

    def _post_standard(self, client: TestClient):
        return client.post(
            "/api/standards",
            json={
                "name": "n",
                "description": "d",
                "severity": "low",
                "category": "general",
                "validation_criteria": "c",
            },
        )

    def test_validator_still_cannot_write_the_library(self):
        assert self._post_standard(_client_as("validator")).status_code == 403

    @pytest.mark.parametrize("role", ["curator", "admin"])
    def test_curator_roles_can_write_the_library(self, role):
        assert self._post_standard(_client_as(role)).status_code != 403
