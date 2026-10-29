"""Cognito ID-token verification, exercised with real RSA-signed tokens.

These sign tokens with a locally generated key and stub the JWKS fetch, so
``verify_token`` runs its true code path — signature, issuer, expiry, audience,
token_use — with no AWS and no network.

Why this file exists
--------------------
Every SAML/SSO sign-in was rejected with 401 while password logins worked. The
cause was ``at_hash``: python-jose, the JWT library then in use, validated that
claim by default and raised "No access_token provided to compare against at_hash
claim" when the claim was
present but no access token was passed to compare against. ID tokens from the
hosted-UI OAuth2 code flow (i.e. all federated sign-ins) CARRY at_hash; tokens
from direct SRP/password auth do NOT. So the bug was invisible to every existing
test, because none of them used a token shaped like a real SSO token.

The at_hash-bearing case below is therefore the regression guard. The rest pin
the checks that actually establish trust, so that disabling at_hash validation
cannot quietly become "disable the parts that matter".
"""

from __future__ import annotations

import time
from typing import Any

import base64
import hashlib
import hmac
import json

import jwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

POOL_ID = "us-east-1_TestPool"
REGION = "us-east-1"
WEB_CLIENT_ID = "test-web-client-id"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
KID = "test-key-1"


@pytest.fixture(scope="module")
def signing_key() -> Any:
    return rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )


@pytest.fixture(scope="module")
def private_pem(signing_key) -> str:
    from cryptography.hazmat.primitives import serialization

    return signing_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="module")
def jwks(signing_key) -> dict:
    """The JWKS document Cognito would serve for this key."""
    key_dict = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key())
    )
    key_dict.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [key_dict]}


@pytest.fixture(autouse=True)
def auth_env(monkeypatch, jwks):
    """Point the verifier at the test pool and stub the JWKS fetch."""
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL_ID)
    monkeypatch.setenv("COGNITO_WEB_APP_CLIENT_ID", WEB_CLIENT_ID)

    from app.api import auth as auth_mod

    auth_mod._get_jwks.cache_clear()
    monkeypatch.setattr(auth_mod, "_get_jwks", lambda: jwks)
    yield


def _token(private_pem: str, **overrides) -> str:
    """A Cognito-shaped ID token. Overrides tweak individual claims."""
    now = int(time.time())
    claims = {
        "sub": "11111111-2222-3333-4444-555555555555",
        "aud": WEB_CLIENT_ID,
        "iss": ISSUER,
        "token_use": "id",
        "email": "user@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": KID})


# --- the regression: SSO-shaped tokens carry at_hash ------------------------


def test_accepts_an_sso_token_carrying_at_hash(private_pem):
    """An ID token from the hosted-UI OAuth2 code flow includes at_hash.

    python-jose validated at_hash by default and raised when it could not be
    compared, which 401'd every federated sign-in. The verifier must accept
    this token: it is a resource server holding one bearer ID token, with no
    paired access token to hash.
    """
    from app.api.auth import verify_token

    token = _token(private_pem, at_hash="cH7nQ0nJb1kx2FhVYQ8Zzg")
    claims = verify_token(token)
    assert claims["sub"] == "11111111-2222-3333-4444-555555555555"
    assert claims["token_use"] == "id"


def test_at_hash_value_is_not_itself_trusted(private_pem):
    """Any at_hash value passes, because the claim is not what establishes
    trust here — the signature is. Pinned so a future reader does not mistake
    the skip for a hole: a forged at_hash on an unsigned token still fails."""
    from app.api.auth import verify_token

    assert verify_token(_token(private_pem, at_hash="obviously-not-a-real-hash"))


def test_accepts_a_password_flow_token_without_at_hash(private_pem):
    """Direct SRP/password auth omits at_hash entirely. This path always
    worked; it must keep working."""
    from app.api.auth import verify_token

    assert verify_token(_token(private_pem))["email"] == "user@example.com"


# --- the checks that do establish trust must still reject -------------------


def test_rejects_a_token_signed_by_another_key(private_pem):
    """A valid-looking token signed by a key outside the pool's JWKS."""
    from app.api.auth import verify_token

    other = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    from cryptography.hazmat.primitives import serialization

    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    now = int(time.time())
    forged = jwt.encode(
        {
            "sub": "attacker",
            "aud": WEB_CLIENT_ID,
            "iss": ISSUER,
            "token_use": "id",
            "exp": now + 3600,
            "at_hash": "whatever",
        },
        other_pem,
        algorithm="RS256",
        headers={"kid": KID},
    )
    with pytest.raises(HTTPException) as exc:
        verify_token(forged)
    assert exc.value.status_code == 401


def test_rejects_a_foreign_issuer(private_pem):
    from app.api.auth import verify_token

    with pytest.raises(HTTPException) as exc:
        verify_token(_token(private_pem, iss="https://evil.example.com/"))
    assert exc.value.status_code == 401


def test_rejects_an_audience_outside_the_allowed_client(private_pem):
    from app.api.auth import verify_token

    with pytest.raises(HTTPException) as exc:
        verify_token(_token(private_pem, aud="some-other-app-client"))
    assert exc.value.status_code == 401
    assert "audience" in exc.value.detail.lower()


def test_rejects_an_expired_token(private_pem):
    from app.api.auth import verify_token

    now = int(time.time())
    with pytest.raises(HTTPException) as exc:
        verify_token(_token(private_pem, iat=now - 7200, exp=now - 3600))
    assert exc.value.status_code == 401


def test_rejects_an_access_token(private_pem):
    """Only ID tokens carry the claims routes read (email, groups). An access
    token must not be usable as a credential here."""
    from app.api.auth import verify_token

    with pytest.raises(HTTPException) as exc:
        verify_token(_token(private_pem, token_use="access"))  # nosec B106 - token type, not a password
    assert exc.value.status_code == 401
    assert "id token" in exc.value.detail.lower()


def test_rejects_hs256_signed_with_the_public_key(signing_key):
    """The algorithm-confusion attack (CVE-2024-33663, CVE-2026-85394): an
    attacker holding the pool's PUBLIC key signs an HS256 token with that key as
    the HMAC secret. The verifier pins RS256, so it must refuse the token."""
    from app.api.auth import verify_token

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    public_pem = signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    payload = b64(json.dumps({
        "sub": "attacker", "aud": WEB_CLIENT_ID, "iss": ISSUER,
        "token_use": "id", "exp": now + 3600,
    }).encode())
    signature = b64(hmac.new(
        public_pem, f"{header}.{payload}".encode(), hashlib.sha256
    ).digest())

    with pytest.raises(HTTPException) as exc:
        verify_token(f"{header}.{payload}.{signature}")
    assert exc.value.status_code == 401


def test_rejects_a_token_without_an_expiry(private_pem):
    """exp is required, not merely checked when present."""
    from app.api.auth import verify_token

    token = jwt.encode(
        {"sub": "u", "aud": WEB_CLIENT_ID, "iss": ISSUER, "token_use": "id"},
        private_pem, algorithm="RS256", headers={"kid": KID},
    )
    with pytest.raises(HTTPException) as exc:
        verify_token(token)
    assert exc.value.status_code == 401
