# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fixtures for end-to-end tests against a DEPLOYED environment.

These tests are skipped unless `--run-e2e` is passed (see tests/conftest.py).
They need:

  E2E_API_URL          base URL of the deployed API. Use the CloudFront URL:
                       the ALB rejects requests that don't carry CloudFront's
                       origin-verify header, so hitting it directly returns 403.
  E2E_COGNITO_CLIENT_ID  Cognito *web* app client id (USER_PASSWORD_AUTH enabled)
  E2E_CURATOR_EMAIL / E2E_CURATOR_PASSWORD    a seeded curator login
  E2E_VIEWER_EMAIL  / E2E_VIEWER_PASSWORD     a seeded viewer login

`deploy.sh seed-users` creates the four per-role logins. Anything missing simply
skips the affected test rather than failing, so a partially configured
environment still runs what it can.
"""

from __future__ import annotations

import os

import pytest

requests = pytest.importorskip("requests", reason="E2E needs requests")


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value else None


@pytest.fixture(scope="session")
def api_url() -> str:
    url = _env("E2E_API_URL")
    if not url:
        pytest.skip("E2E_API_URL not set")
    return url.rstrip("/")


def _id_token(email_var: str, password_var: str) -> str:
    """Exchange a seeded user's password for a Cognito ID token."""
    client_id = _env("E2E_COGNITO_CLIENT_ID")
    email = _env(email_var)
    password = _env(password_var)
    if not (client_id and email and password):
        pytest.skip(f"need E2E_COGNITO_CLIENT_ID + {email_var} + {password_var}")

    boto3 = pytest.importorskip("boto3", reason="E2E auth needs boto3")
    client = boto3.client("cognito-idp", region_name=os.getenv("AWS_REGION", "us-east-1"))
    try:
        resp = client.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=client_id,
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
    except Exception as exc:  # noqa: BLE001 — a login failure is a skip, not a bug
        pytest.skip(f"Cognito auth failed for {email}: {exc}")
    token = resp.get("AuthenticationResult", {}).get("IdToken")
    if not token:
        pytest.skip(f"no IdToken returned for {email} (challenge required?)")
    return token


@pytest.fixture(scope="session")
def curator_token() -> str:
    return _id_token("E2E_CURATOR_EMAIL", "E2E_CURATOR_PASSWORD")


@pytest.fixture(scope="session")
def viewer_token() -> str:
    return _id_token("E2E_VIEWER_EMAIL", "E2E_VIEWER_PASSWORD")


class ApiClient:
    """Thin authenticated HTTP helper — keeps the tests readable."""

    def __init__(self, base_url: str, token: str | None = None, timeout: int = 120):
        self.base_url = base_url
        self.token = token
        self.timeout = timeout

    def _headers(self, extra: dict | None = None) -> dict:
        headers = dict(extra or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path: str, **kw):
        return requests.get(f"{self.base_url}{path}", headers=self._headers(),
                            timeout=self.timeout, **kw)

    def post(self, path: str, *, json=None, files=None, data=None):
        return requests.post(
            f"{self.base_url}{path}",
            headers=self._headers({"Content-Type": "application/json"} if json else None),
            json=json, files=files, data=data, timeout=self.timeout,
        )

    def delete(self, path: str):
        return requests.delete(f"{self.base_url}{path}", headers=self._headers(),
                               timeout=self.timeout)


@pytest.fixture
def curator(api_url, curator_token) -> ApiClient:
    return ApiClient(api_url, curator_token)


@pytest.fixture
def viewer(api_url, viewer_token) -> ApiClient:
    return ApiClient(api_url, viewer_token)


@pytest.fixture
def anonymous(api_url) -> ApiClient:
    return ApiClient(api_url)


@pytest.fixture
def png_bytes() -> bytes:
    """A small, real PNG (64x64 solid colour) for icon-upload tests."""
    Image = pytest.importorskip("PIL.Image", reason="needs pillow")
    import io
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (230, 126, 34)).save(buf, format="PNG")
    return buf.getvalue()
