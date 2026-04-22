"""Shared Azure-AD device-flow machinery.

Snowflake (via External OAuth), Azure DevOps, and plain Azure all use
the same flow — MSAL handles the polling. Differences are the scope,
tenant, client_id, and any provider-specific post-processing. This
module factors out the common logic so each provider is a handful of
config values.
"""

from __future__ import annotations

import logging
import time

from ..cache import TokenRecord
from ..device_flow import DevicePrompt, emit_prompt

logger = logging.getLogger(__name__)


def run_aad_device_flow(
    *,
    provider_name: str,
    tenant: str,
    client_id: str,
    scope: str,
    app=None,   # injectable for tests
) -> TokenRecord:
    """Run the full device code flow against Azure AD. Blocks until the
    user completes login or MSAL reports flow expiry. Emits a
    DevicePrompt with the verification URL and user_code as soon as the
    flow is initiated."""
    if app is None:
        import msal
        app = msal.PublicClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant}",
        )

    flow = app.initiate_device_flow(scopes=[scope])
    if "user_code" not in flow:
        raise RuntimeError(
            f"{provider_name}: device flow init failed: "
            f"{flow.get('error_description') or flow}"
        )

    emit_prompt(DevicePrompt(
        provider=provider_name,
        verification_uri=flow["verification_uri"],
        user_code=flow["user_code"],
        expires_in=flow.get("expires_in", 900),
        message=flow.get("message", ""),
    ))
    logger.info("%s: device flow pending (user_code=%s)", provider_name, flow["user_code"])

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(
            f"{provider_name}: device flow failed: "
            f"{result.get('error_description') or result.get('error')}"
        )

    expires_at = time.time() + int(result.get("expires_in", 3600))
    return TokenRecord(token=result["access_token"], expires_at=expires_at)
