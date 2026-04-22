"""Parse Azure App Service EasyAuth headers into a UserIdentity.

Used by agent_frontend only. EasyAuth sits in front of the frontend and
injects these headers after a successful SSO login. The frontend extracts
the raw ID token (X-MS-TOKEN-AAD-ID-TOKEN) and forwards it to the runtime
as `Authorization: Bearer`, where `validate_jwt` does the actual trust
check. The principal headers parsed here are used only for display/UX
before the Bearer reaches the runtime.

Dev-mode bypass: when AUTH_DEV_MODE=1, synthesizes a fake identity so
local development doesn't need an EasyAuth setup. The bypass is strictly
opt-in and decoupled from storage mode.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Mapping, Optional

from .identity import UserIdentity

logger = logging.getLogger(__name__)


def _dev_user() -> UserIdentity:
    return UserIdentity(
        user_id=os.getenv("AUTH_DEV_USER_ID", "00000000-0000-0000-0000-000000000001"),
        principal_name=os.getenv("AUTH_DEV_USER_EMAIL", "dev@example.com"),
        display_name=os.getenv("AUTH_DEV_USER_NAME", "Dev User"),
        is_authenticated=True,
        raw_token=None,
    )


def parse_easyauth_headers(headers: Mapping[str, str]) -> Optional[UserIdentity]:
    """Extract a UserIdentity from EasyAuth headers.

    Returns None if no authenticated identity is present. Returns a
    synthetic dev identity when AUTH_DEV_MODE=1. The returned
    UserIdentity carries the raw ID token in `raw_token` when EasyAuth
    provided one — frontend forwards that as Bearer to the runtime.
    """
    if os.getenv("AUTH_DEV_MODE", "").lower() in ("1", "true", "yes"):
        return _dev_user()

    oid = headers.get("X-MS-CLIENT-PRINCIPAL-ID") or headers.get("x-ms-client-principal-id")
    principal_name = (
        headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
        or headers.get("x-ms-client-principal-name")
        or ""
    )
    principal_raw = (
        headers.get("X-MS-CLIENT-PRINCIPAL")
        or headers.get("x-ms-client-principal")
        or ""
    )
    id_token = (
        headers.get("X-MS-TOKEN-AAD-ID-TOKEN")
        or headers.get("x-ms-token-aad-id-token")
        or ""
    )

    if not oid:
        return None

    display_name = ""
    if principal_raw:
        try:
            decoded = json.loads(base64.b64decode(principal_raw).decode("utf-8"))
            for claim in decoded.get("claims", []):
                if claim.get("typ") == "name":
                    display_name = claim.get("val", "")
                    break
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
            # Header is present but malformed. Degrade gracefully — downstream
            # JWT validation is the real gatekeeper.
            logger.warning("failed to decode X-MS-CLIENT-PRINCIPAL: %s", e)

    return UserIdentity(
        user_id=oid,
        principal_name=principal_name,
        display_name=display_name,
        is_authenticated=True,
        raw_token=id_token or None,
    )
