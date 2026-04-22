"""Snowflake via Azure AD External OAuth + device code flow.

Deployment prerequisite: Snowflake is configured with an `External OAuth`
security integration whose authorization server is Azure AD. That
integration exposes a resource (scope) we request a token for. The
returned access token is sent to Snowflake as Authorization: Bearer.

Config fields in auth.json:
    mode:      "device_code"
    type:      "snowflake"
    tenant:    AAD tenant ID
    client_id: AAD app registration client_id (the one consenting the
               Snowflake External OAuth scope)
    scope:     the Snowflake External OAuth resource scope — typically
               "api://<snowflake-external-oauth-app-id>/session:scope:ANALYST"
               or the tenant-specific format your integration uses
    account:   Snowflake account identifier (e.g. xy12345.us-east-1) —
               not used in the AAD flow; stored so MCP servers /
               connection helpers can read it from Provider.account.

Non-Azure-AD Snowflake OAuth paths (native Snowflake OAuth, key-pair
auth, PAT) are out of scope for v1 — add them as separate providers
with their own `type`.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..cache import TokenRecord
from ._aad_device import run_aad_device_flow
from .base import Provider, ProviderMode

logger = logging.getLogger(__name__)


class SnowflakeDeviceProvider(Provider):
    mode: ProviderMode = "device_code"

    def __init__(
        self,
        name: str,
        *,
        tenant: str,
        client_id: str,
        scope: str,
        account: str,
        cache=None,
    ):
        super().__init__(name, cache=cache)
        self._tenant = tenant
        self._client_id = client_id
        self._scope = scope
        self.account = account     # exposed so connection code can read it
        self._app = None

    def _fetch(self, user_id: Optional[str]) -> TokenRecord:
        if not user_id:
            raise RuntimeError(
                f"SnowflakeDeviceProvider({self.name!r})._fetch called without user_id"
            )
        return run_aad_device_flow(
            provider_name=self.name,
            tenant=self._tenant,
            client_id=self._client_id,
            scope=self._scope,
            app=self._app,
        )
