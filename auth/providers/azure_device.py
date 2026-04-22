"""Azure AD device code provider.

OAuth2 device authorization grant against Azure AD. User completes login
in a browser tab; this provider polls Azure until the flow resolves.

Blocking: `_fetch` runs on the agent-loop worker thread. `initiate_device_flow`
returns immediately with URL+user_code; we emit that via the device_flow
callback, then `acquire_token_by_device_flow` blocks until Azure returns
a token or the flow times out.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from ..cache import TokenRecord
from ..device_flow import DevicePrompt, emit_prompt
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
        self._app = None    # lazy MSAL init

    def _get_app(self):
        if self._app is None:
            import msal
            self._app = msal.PublicClientApplication(
                self._client_id,
                authority=f"https://login.microsoftonline.com/{self._tenant}",
            )
        return self._app

    def _fetch(self, user_id: Optional[str]) -> TokenRecord:
        if not user_id:
            # Provider.get_valid_token guarantees non-None for device_code
            # mode, but defensive.
            raise RuntimeError(
                f"AzureDeviceProvider({self.name!r})._fetch called without user_id"
            )

        app = self._get_app()
        flow = app.initiate_device_flow(scopes=[self._scope])

        if "user_code" not in flow:
            raise RuntimeError(
                f"azure device flow init failed for provider {self.name!r}: "
                f"{flow.get('error_description') or flow}"
            )

        emit_prompt(DevicePrompt(
            provider=self.name,
            verification_uri=flow["verification_uri"],
            user_code=flow["user_code"],
            expires_in=flow.get("expires_in", 900),
            message=flow.get("message", ""),
        ))
        logger.info(
            "azure device flow pending: provider=%s user=%s",
            self.name, user_id,
        )

        # Blocks until the user completes login or the flow times out.
        result = app.acquire_token_by_device_flow(flow)

        if "access_token" not in result:
            raise RuntimeError(
                f"azure device flow failed for provider {self.name!r}: "
                f"{result.get('error_description') or result.get('error')}"
            )

        expires_at = time.time() + int(result.get("expires_in", 3600))
        return TokenRecord(token=result["access_token"], expires_at=expires_at)
