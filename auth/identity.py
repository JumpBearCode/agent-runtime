"""UserIdentity + JWT validation.

Runtime uses `validate_jwt` as its gatekeeper: every request arrives with
`Authorization: Bearer <jwt>`, this module verifies signature, issuer,
audience, expiry, and the required scope, and returns a canonical
UserIdentity. Nothing else in the codebase should parse or trust raw
headers/claims directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)


class InvalidToken(Exception):
    """Raised when a JWT fails validation. Message is safe to log; do NOT
    include the token itself in the message — callers log it as-is."""


@dataclass(frozen=True)
class UserIdentity:
    """Canonical in-process representation of an authenticated user.

    Built in exactly two places: `validate_jwt` (runtime) and
    `parse_easyauth_headers` (frontend + dev-mode bypass). Every other
    layer (engine, tools, cache) consumes this.
    """
    user_id:            str          # stable OID from AAD
    principal_name:     str          # UPN / email — may be empty in some tenants
    display_name:       str          # human-readable name — may be empty
    is_authenticated:   bool
    # Optional — kept for device-flow-style future providers that might
    # want to see the raw token. Not used for OBO in v1.
    raw_token:          Optional[str] = None


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> PyJWKClient:
    """One PyJWKClient per JWKS URL. Internal key cache (~1h) is on by
    default; we don't need a second layer."""
    return PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)


def validate_jwt(
    token: str,
    *,
    tenant_id: str,
    audience: str,
    required_scope: Optional[str] = None,
) -> UserIdentity:
    """Verify a bearer JWT and return the UserIdentity it asserts.

    Checks: RS256 signature (via Azure AD JWKS), iss, aud, exp.
    If `required_scope` is given, also checks that it appears in the
    token's `scp` (space-separated) or `roles` claim.

    Raises `InvalidToken` on any failure — callers translate to 401.
    """
    if not token:
        raise InvalidToken("empty token")

    jwks_url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    # v2.0 issuer format. If your app registration is v1, issuer is
    # "https://sts.windows.net/{tenant}/". Assume v2 by default.
    issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"

    try:
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
        )
    except jwt.ExpiredSignatureError:
        raise InvalidToken("token expired")
    except jwt.InvalidAudienceError:
        raise InvalidToken("invalid audience")
    except jwt.InvalidIssuerError:
        raise InvalidToken("invalid issuer")
    except jwt.InvalidSignatureError:
        raise InvalidToken("invalid signature")
    except jwt.PyJWTError as e:
        raise InvalidToken(f"jwt decode failed: {type(e).__name__}")

    if required_scope is not None:
        scp = set((claims.get("scp") or "").split())
        roles = set(claims.get("roles") or [])
        if required_scope not in scp and required_scope not in roles:
            raise InvalidToken(f"missing required scope: {required_scope}")

    oid = claims.get("oid") or claims.get("sub")
    if not oid:
        raise InvalidToken("token has no oid/sub claim")

    return UserIdentity(
        user_id=oid,
        principal_name=(
            claims.get("preferred_username")
            or claims.get("upn")
            or claims.get("email")
            or ""
        ),
        display_name=claims.get("name", ""),
        is_authenticated=True,
        raw_token=token,
    )
