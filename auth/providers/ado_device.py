"""Azure DevOps via Azure AD device code flow.

ADO's API accepts AAD tokens scoped to its resource ID. The well-known
ADO resource ID is `499b84ac-1321-427f-aa17-267ca6975798` — default scope
is `499b84ac-1321-427f-aa17-267ca6975798/.default`.

Config fields in auth.json:
    mode:      "device_code"
    type:      "ado"
    tenant:    AAD tenant ID
    client_id: AAD app registration client_id (with delegated permission
               on the Azure DevOps API)
    scope:     defaults to ADO's well-known resource scope; override per
               org/tenant if needed
    org:       ADO organization name (e.g. "mycompany") — not used in
               the AAD flow; exposed so connection code can read it.

Alternative: PAT-based ADO access bypasses AAD entirely. Not covered in
v1 — add as a separate `type: "ado-pat"` provider if needed.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..cache import TokenRecord
from ._aad_device import run_aad_device_flow
from .base import Provider, ProviderMode

logger = logging.getLogger(__name__)


_ADO_DEFAULT_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"


class AdoDeviceProvider(Provider):
    mode: ProviderMode = "device_code"

    def __init__(
        self,
        name: str,
        *,
        tenant: str,
        client_id: str,
        org: str,
        scope: Optional[str] = None,
        cache=None,
    ):
        super().__init__(name, cache=cache)
        self._tenant = tenant
        self._client_id = client_id
        self._scope = scope or _ADO_DEFAULT_SCOPE
        self.org = org
        self._app = None

    def _fetch(self, user_id: Optional[str]) -> TokenRecord:
        if not user_id:
            raise RuntimeError(
                f"AdoDeviceProvider({self.name!r})._fetch called without user_id"
            )
        return run_aad_device_flow(
            provider_name=self.name,
            tenant=self._tenant,
            client_id=self._client_id,
            scope=self._scope,
            app=self._app,
        )
