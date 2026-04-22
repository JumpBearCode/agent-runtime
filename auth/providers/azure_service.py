"""Azure service-mode provider — wraps DefaultAzureCredential.

Container-level identity (Managed Identity in production, developer CLI
login locally). Same credential for every user. Token lifetime is
managed by the Azure SDK's internal cache; our TokenCache layers a
consistent interface on top.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..cache import TokenRecord
from .base import Provider, ProviderMode

logger = logging.getLogger(__name__)


class AzureServiceProvider(Provider):
    mode: ProviderMode = "service"

    def __init__(self, name: str, *, scope: str, cache=None):
        super().__init__(name, cache=cache)
        self._scope = scope
        # Lazy init so tests can construct without hitting Azure.
        self._credential = None

    def _get_credential(self):
        if self._credential is None:
            from azure.identity import DefaultAzureCredential
            self._credential = DefaultAzureCredential()
        return self._credential

    def _fetch(self, user_id: Optional[str]) -> TokenRecord:
        # user_id intentionally ignored — service mode uses container ID.
        token = self._get_credential().get_token(self._scope)
        return TokenRecord(token=token.token, expires_at=float(token.expires_on))
