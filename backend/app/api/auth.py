# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cognito JWT verification for FastAPI routes.

The reference market-surveillance project offloads JWT verification to API
Gateway. We don't have API Gateway in front of FastAPI — the ALB routes
straight to the container — so verification has to live here.

Pattern:
- Cache the JWKS (Cognito's public signing keys) once per process.
- Bust the cache and refetch if a token references an unknown `kid`,
  which happens after key rotation.
- Verify the token's signature, expiry, and audience claim.
- Return a `dict` of claims so route handlers can read sub/email/etc.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

import httpx
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt

logger = logging.getLogger(__name__)

# Lazily resolve env vars so import-time misconfiguration doesn't crash the app.
def _region() -> str:
    return os.getenv("AWS_REGION", "us-east-1")

def _user_pool_id() -> str:
    return os.getenv("COGNITO_USER_POOL_ID", "")

def _client_id() -> str:
    return os.getenv("COGNITO_WEB_APP_CLIENT_ID", "")

def _issuer() -> str:
    return f"https://cognito-idp.{_region()}.amazonaws.com/{_user_pool_id()}"

def _jwks_url() -> str:
    return f"{_issuer()}/.well-known/jwks.json"


bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    """Fetch and cache the Cognito JWKS. Cache busts on `kid` miss."""
    url = _jwks_url()
    resp = httpx.get(url, timeout=5.0)
    resp.raise_for_status()
    return resp.json()


def _find_key(jwks: dict, kid: str) -> Optional[dict]:
    return next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)


def verify_token(token: str) -> dict:
    """Decode and verify a Cognito ID token. Raises HTTPException(401) on failure.

    Accepts tokens from either sign-in path against this user pool: direct
    SRP/password auth and the hosted-UI OAuth2 code flow used for SAML/SSO.
    Both land on the same app client, so the signature/issuer/audience checks
    are identical; only the presence of the ``at_hash`` claim differs (see
    below).
    """
    if not _user_pool_id() or not _client_id():
        # Misconfigured deployment — fail loudly instead of silently allowing.
        raise HTTPException(
            status_code=503,
            detail="Auth not configured: COGNITO_USER_POOL_ID and COGNITO_WEB_APP_CLIENT_ID required",
        )

    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            raise HTTPException(status_code=401, detail="Token missing 'kid' header")

        jwks = _get_jwks()
        key = _find_key(jwks, kid)
        if not key:
            # Likely key rotation — refetch JWKS once.
            _get_jwks.cache_clear()
            jwks = _get_jwks()
            key = _find_key(jwks, kid)
        if not key:
            raise HTTPException(status_code=401, detail="Unknown signing key")

        # The algorithm is pinned to RS256 and the key is built from the JWK as
        # an RSA public key, so a token cannot choose its own algorithm (the
        # HS256-with-the-public-key confusion attack).
        #
        # `at_hash` is deliberately not checked. ID tokens from the hosted-UI
        # OAuth2 code flow — every SAML/SSO sign-in — carry it, while direct
        # SRP/password tokens do not. It exists so an OAuth *client* can confirm
        # the ID token and access token it received are a pair; this service is
        # a resource server handed one ID token as a bearer credential, with no
        # access token to hash. (python-jose checked it by default, which once
        # 401'd every federated login.) Trust comes from the RS256 signature
        # against Cognito's JWKS, the issuer, the expiry, the audience, and
        # token_use — all enforced.
        claims = jwt.decode(
            token,
            jwt.PyJWK(key, algorithm="RS256").key,
            algorithms=["RS256"],
            audience=_client_id(),
            issuer=_issuer(),
            options={"require": ["exp", "iss", "aud"]},
        )

        # Only ID tokens carry the profile claims routes rely on; reject
        # access tokens (token_use == "access") outright.
        if claims.get("token_use") != "id":
            logger.warning(
                "token rejected: token_use=%r (expected 'id'); aud=%r",
                claims.get("token_use"), claims.get("aud"),
            )
            raise HTTPException(status_code=401, detail="Expected an ID token")

        return claims
    except HTTPException:
        raise
    except jwt.PyJWTError as exc:
        # Signature / issuer / expiry / audience failures land here. Log the
        # reason — otherwise an issuer or signing-key mismatch is
        # indistinguishable from any other 401 in the access log.
        logger.warning("JWT rejected: %s", exc)
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
) -> dict:
    """FastAPI dependency — extracts and verifies the Cognito ID token."""
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return verify_token(credentials.credentials)
