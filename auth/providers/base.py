"""Provider ABC — how auth/ knows to fetch a token for a given identity.

Every provider has a `mode`:
- "service"     — container-level credential, user_id is ignored
- "device_code" — per-user credential, cache miss raises AuthRequired

Providers own their fetch logic. Caching (TokenCache) is shared and
sits above; `get_valid_token` combines both.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal, Optional

from ..cache import CacheKey, TokenCache, TokenRecord, default_cache


ProviderMode = Literal["service", "device_code"]


class AuthRequired(Exception):
    """Raised by device_code providers on cache miss.

    The runtime catches this, pushes a `device_flow_request` SSE event to
    the frontend, waits for the user to complete the flow, then retries.
    See agent_runtime/engine.py for the HITL-style pending-action wiring.
    """

    def __init__(
        self,
        provider: str,
        verification_uri: str,
        user_code: str,
        device_code: str,
        expires_in: int,
        interval: int,
    ):
        super().__init__(f"device login required for {provider}")
        self.provider = provider
        self.verification_uri = verification_uri
        self.user_code = user_code
        self.device_code = device_code
        self.expires_in = expires_in
        self.interval = interval


class Provider(ABC):
    """One credential source, e.g. "azure-service" or "snowflake".

    Subclasses implement `_fetch`; `get_valid_token` wraps it with cache
    lookup. Mode is set by the subclass (or config), not per instance.
    """

    mode: ProviderMode

    def __init__(self, name: str, *, cache: Optional[TokenCache] = None):
        self.name = name
        self._cache = cache or default_cache()

    # ── public ─────────────────────────────────────────────────────────

    def get_valid_token(self, user_id: Optional[str]) -> TokenRecord:
        """Return a non-expired token, fetching if necessary. May raise
        AuthRequired (device_code mode, cache miss)."""
        key = self._cache_key(user_id)
        rec = self._cache.get(key)
        if rec is not None:
            return rec
        rec = self._fetch(user_id)
        self._cache.put(key, rec)
        return rec

    def invalidate(self, user_id: Optional[str]) -> None:
        self._cache.invalidate(self._cache_key(user_id))

    # ── subclass API ───────────────────────────────────────────────────

    @abstractmethod
    def _fetch(self, user_id: Optional[str]) -> TokenRecord:
        """Acquire a fresh token. Service-mode providers ignore user_id.
        Device-code-mode providers raise AuthRequired on first call and
        return a cached token on subsequent calls after the user
        completes the flow."""
        ...

    # ── helpers ────────────────────────────────────────────────────────

    def _cache_key(self, user_id: Optional[str]) -> CacheKey:
        if self.mode == "device_code":
            if not user_id:
                raise RuntimeError(
                    f"provider {self.name!r} is device_code mode but no "
                    f"user_id was passed — this is a runtime bug, not a "
                    f"misconfiguration"
                )
            return (user_id, self.name)
        # service mode: no user key
        return (None, self.name)
