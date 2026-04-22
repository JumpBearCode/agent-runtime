"""Cache, ContextualCredential, and provider registry."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from auth.cache import TokenCache, TokenRecord
from auth.contextual import ContextualCredential, with_auth
from auth.providers.base import Provider, ProviderMode
from auth.providers.registry import (
    build_providers,
    clear_providers,
    get_provider,
)


# ── TokenRecord / TokenCache ────────────────────────────────────────────────

def test_token_record_expiry():
    fresh = TokenRecord(token="t", expires_at=time.time() + 600)
    stale = TokenRecord(token="t", expires_at=time.time() + 10)
    assert not fresh.is_expired(leeway_seconds=60)
    assert stale.is_expired(leeway_seconds=60)


def test_cache_put_get_invalidate():
    cache = TokenCache()
    key = ("u1", "azure")
    rec = TokenRecord(token="abc", expires_at=time.time() + 600)

    assert cache.get(key) is None
    cache.put(key, rec)
    assert cache.get(key) is rec

    cache.invalidate(key)
    assert cache.get(key) is None


def test_cache_hides_expired():
    cache = TokenCache()
    cache.put(("u", "p"), TokenRecord(token="t", expires_at=time.time() - 5))
    assert cache.get(("u", "p")) is None   # expired → treated as miss


def test_cache_per_user_isolation():
    cache = TokenCache()
    cache.put(("alice", "azure"), TokenRecord(token="A", expires_at=time.time() + 600))
    cache.put(("bob", "azure"),   TokenRecord(token="B", expires_at=time.time() + 600))
    assert cache.get(("alice", "azure")).token == "A"
    assert cache.get(("bob",   "azure")).token == "B"


def test_cache_invalidate_user():
    cache = TokenCache()
    cache.put(("alice", "azure"),     TokenRecord("A1", time.time() + 600))
    cache.put(("alice", "snowflake"), TokenRecord("A2", time.time() + 600))
    cache.put(("bob",   "azure"),     TokenRecord("B",  time.time() + 600))

    cache.invalidate_user("alice")
    assert cache.get(("alice", "azure")) is None
    assert cache.get(("alice", "snowflake")) is None
    assert cache.get(("bob",   "azure")) is not None


# ── Provider ABC via a fake subclass ────────────────────────────────────────

class _FakeService(Provider):
    mode: ProviderMode = "service"

    def __init__(self, name: str, *, cache=None):
        super().__init__(name, cache=cache)
        self.fetches = 0

    def _fetch(self, user_id):
        self.fetches += 1
        return TokenRecord(token=f"tok-{self.fetches}", expires_at=time.time() + 600)


class _FakeDevice(Provider):
    mode: ProviderMode = "device_code"

    def __init__(self, name: str, *, cache=None):
        super().__init__(name, cache=cache)

    def _fetch(self, user_id):
        return TokenRecord(token=f"tok-{user_id}", expires_at=time.time() + 600)


def test_provider_caches_tokens_across_calls():
    cache = TokenCache()
    p = _FakeService("svc", cache=cache)

    a = p.get_valid_token(None)
    b = p.get_valid_token(None)
    assert a.token == b.token == "tok-1"
    assert p.fetches == 1   # second call served from cache


def test_provider_refetches_after_invalidate():
    cache = TokenCache()
    p = _FakeService("svc", cache=cache)
    p.get_valid_token(None)
    p.invalidate(None)
    p.get_valid_token(None)
    assert p.fetches == 2


def test_device_mode_requires_user_id():
    p = _FakeDevice("dev", cache=TokenCache())
    with pytest.raises(RuntimeError, match="device_code mode but no user_id"):
        p.get_valid_token(None)


def test_device_mode_keys_by_user():
    cache = TokenCache()
    p = _FakeDevice("dev", cache=cache)
    assert p.get_valid_token("alice").token == "tok-alice"
    assert p.get_valid_token("bob").token == "tok-bob"
    # Alice's token wasn't overwritten:
    assert p.get_valid_token("alice").token == "tok-alice"


def test_service_mode_shares_across_users():
    """Service providers hash by None regardless of which user called."""
    cache = TokenCache()
    p = _FakeService("svc", cache=cache)
    p.get_valid_token("alice")
    p.get_valid_token("bob")
    assert p.fetches == 1   # Bob got Alice's cached token, as designed


# ── Registry ────────────────────────────────────────────────────────────────

def test_registry_builds_and_retrieves():
    clear_providers()
    build_providers({
        "azure": {"mode": "service", "scope": "https://management.azure.com/.default"},
    })
    provider = get_provider("azure")
    assert provider.name == "azure"
    assert provider.mode == "service"


def test_registry_unknown_name_raises_keyerror():
    clear_providers()
    build_providers({})
    with pytest.raises(KeyError, match="no auth provider"):
        get_provider("ghost")


def test_registry_rejects_unknown_mode():
    clear_providers()
    with pytest.raises(ValueError, match="unknown mode"):
        build_providers({"weird": {"mode": "magic"}})


# ── ContextualCredential ────────────────────────────────────────────────────

def test_contextual_credential_reads_thread_local():
    # Skip if azure-core isn't installed in the test env.
    pytest.importorskip("azure.core.credentials")

    cred = ContextualCredential()

    ContextualCredential.set_token("my-token", int(time.time()) + 600)
    tok = cred.get_token("scope/.default")
    assert tok.token == "my-token"

    ContextualCredential.clear()
    with pytest.raises(RuntimeError, match="no token in thread-local"):
        cred.get_token("scope/.default")


def test_with_auth_decorator_sets_and_clears():
    pytest.importorskip("azure.core.credentials")

    cred = ContextualCredential()
    seen = []

    @with_auth
    def tool():
        seen.append(cred.get_token("whatever").token)

    tool(_auth_token="abc", _auth_expires_at=int(time.time()) + 600)
    assert seen == ["abc"]

    # After the call, thread-local must be cleared.
    with pytest.raises(RuntimeError):
        cred.get_token("whatever")


def test_with_auth_no_token_means_no_set():
    """Tool called without _auth_token shouldn't leak prior thread state."""
    pytest.importorskip("azure.core.credentials")
    cred = ContextualCredential()

    ContextualCredential.set_token("leftover", int(time.time()) + 600)
    ran = False

    @with_auth
    def tool():
        nonlocal ran
        ran = True
        return cred.get_token("x").token

    # No _auth_token passed → decorator doesn't touch thread-local →
    # tool sees whatever was there before. This is intentional: the
    # decorator is opt-in; tests document the behavior.
    assert tool() == "leftover"
    assert ran
    ContextualCredential.clear()


def test_thread_local_isolation():
    """Two threads must see independent tokens."""
    pytest.importorskip("azure.core.credentials")
    import threading

    cred = ContextualCredential()
    seen: dict[str, str] = {}

    def runner(user: str):
        ContextualCredential.set_token(f"tok-{user}", int(time.time()) + 600)
        # Small yield to maximize interleaving with the other thread.
        time.sleep(0.01)
        seen[user] = cred.get_token("x").token
        ContextualCredential.clear()

    t1 = threading.Thread(target=runner, args=("alice",))
    t2 = threading.Thread(target=runner, args=("bob",))
    t1.start(); t2.start()
    t1.join();  t2.join()

    assert seen == {"alice": "tok-alice", "bob": "tok-bob"}
