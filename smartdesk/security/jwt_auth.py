"""Verification of Supabase Auth access tokens.

The React dashboard authenticates against Supabase directly and sends the
resulting access token as ``Authorization: Bearer <token>``.  Flask never sees
a password and never issues a session cookie.

Supabase projects sign tokens either with the legacy shared HS256 secret or
with an asymmetric key published at a JWKS endpoint.  Both are supported;
whichever is configured is used, and a project can migrate between them
without a code change.

Nothing here trusts a claim it has not verified.  In particular the
``is_platform_admin`` flag is read from OUR database, never from the token,
so a user cannot escalate by editing their own Supabase metadata.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import jwt
from flask import current_app

logger = logging.getLogger(__name__)

_JWKS_CACHE: dict[str, tuple[float, object]] = {}
_JWKS_TTL_SECONDS = 600


class AuthError(Exception):
    """Raised when a token is absent, malformed, expired or untrusted."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    email: str
    raw: dict


def _jwks_client(url: str):
    cached = _JWKS_CACHE.get(url)
    now = time.time()
    if cached and now - cached[0] < _JWKS_TTL_SECONDS:
        return cached[1]
    client = jwt.PyJWKClient(url, cache_keys=True)
    _JWKS_CACHE[url] = (now, client)
    return client


def decode_token(token: str) -> TokenClaims:
    """Verify a Supabase access token and return its claims."""
    if not token:
        raise AuthError("Missing access token")

    audience = current_app.config.get("SUPABASE_JWT_AUDIENCE") or "authenticated"
    jwks_url = current_app.config.get("SUPABASE_JWKS_URL")
    secret = current_app.config.get("SUPABASE_JWT_SECRET")

    options = {"require": ["exp", "sub"], "verify_aud": bool(audience)}

    try:
        if jwks_url:
            signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=audience,
                options=options,
            )
        elif secret:
            payload = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience=audience,
                options=options,
            )
        else:
            # Fail closed. An unconfigured deployment must not accept tokens.
            raise AuthError(
                "Authentication is not configured on this server", status=500
            )
    except AuthError:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Access token has expired") from exc
    except jwt.InvalidTokenError as exc:
        logger.warning("Rejected access token: %s", exc)
        raise AuthError("Invalid access token") from exc
    except Exception as exc:  # network failure reaching JWKS, etc.
        logger.exception("Token verification failed")
        raise AuthError("Could not verify access token", status=503) from exc

    subject = payload.get("sub")
    if not subject:
        raise AuthError("Access token has no subject")

    email = (payload.get("email") or "").strip().lower()
    return TokenClaims(subject=subject, email=email, raw=payload)


def bearer_token_from_header(header_value: str | None) -> str:
    if not header_value:
        raise AuthError("Missing Authorization header")
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("Authorization header must be a Bearer token")
    return parts[1].strip()
