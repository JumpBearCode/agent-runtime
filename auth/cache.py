"""In-process token cache, keyed by (user_id, provider_name).

Per doc/auth-v1-status.md §4: v1 cache is in-memory per uvicorn worker.
Multi-instance deployments rely on sticky sessions. An external-store
impl (Redis, Key Vault) would implement the same interface.

Thread-safety: the agent loop runs in a ThreadPoolExecutor; tool
middleware queries this cache from whichever thread. Mutations are
microsecond-scale, so a single Lock is sufficient.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class TokenRecord:
    """One cached access token with expiry.

    `expires_at` is a unix timestamp in seconds. `is_expired` is the only
    way other code should check — it bakes in the refresh leeway so the
    token doesn't expire mid-request.
    """
    token: str
    expires_at: float

    def is_expired(self, leeway_seconds: int = 60) -> bool:
        return time.time() + leeway_seconds >= self.expires_at


# (user_id_or_none, provider_name). user_id is None for service-mode
# providers — all users share the container's credential.
CacheKey = Tuple[Optional[str], str]


class TokenCache:
    def __init__(self):
        self._store: dict[CacheKey, TokenRecord] = {}
        self._lock = threading.Lock()

    def get(self, key: CacheKey) -> Optional[TokenRecord]:
        with self._lock:
            rec = self._store.get(key)
        if rec is None or rec.is_expired():
            return None
        return rec

    def put(self, key: CacheKey, record: TokenRecord) -> None:
        with self._lock:
            self._store[key] = record

    def invalidate(self, key: CacheKey) -> None:
        with self._lock:
            self._store.pop(key, None)

    def invalidate_user(self, user_id: str) -> None:
        """Remove every entry for one user — used by a future /logout."""
        with self._lock:
            self._store = {
                k: v for k, v in self._store.items() if k[0] != user_id
            }

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level default cache. Runtime uses this; tests can construct
# their own for isolation.
_default_cache = TokenCache()


def default_cache() -> TokenCache:
    return _default_cache
