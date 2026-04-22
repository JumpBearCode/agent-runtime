"""Azure AD device code provider.

Wraps the shared Azure-AD device flow (auth.providers._aad_device).
Produces a token with audience = configured `scope` — for standard Azure
downstream APIs this is e.g. `https://management.azure.com/.default`.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..cache import TokenRecord
from ._aad_device import run_aad_device_flow
from .base import Provider, ProviderMode

logger = logging.getLogger(__name__)


class AzureDeviceProvider(Provider):
    mode: ProviderMode = "device_code"

    def __init__(
        self,
        name: str,
        *,
        tenant: str,
        client_id: str,
        scope: str,
        cache=None,
    ):
        super().__init__(name, cache=cache)
        self._tenant = tenant
        self._client_id = client_id
        self._scope = scope
        self._app = None    # set by tests; None triggers lazy MSAL init

    def _fetch(self, user_id: Optional[str]) -> TokenRecord:
        if not user_id:
            raise RuntimeError(
                f"AzureDeviceProvider({self.name!r})._fetch called without user_id"
            )
        return run_aad_device_flow(
            provider_name=self.name,
            tenant=self._tenant,
            client_id=self._client_id,
            scope=self._scope,
            app=self._app,
        )
