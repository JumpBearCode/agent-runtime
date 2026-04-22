"""AzureDeviceProvider + device-flow callback plumbing.

MSAL is mocked — we don't want tests touching Azure AD. The tests verify:
  - provider initiates flow, emits prompt, returns token when MSAL succeeds
  - provider raises when MSAL fails
  - callback is thread-local (one thread can't see another thread's cb)
  - registry builds AzureDeviceProvider from auth.json shape
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from auth.cache import TokenCache
from auth.device_flow import DevicePrompt, emit_prompt, set_prompt_callback
from auth.providers.azure_device import AzureDeviceProvider
from auth.providers.registry import build_providers, clear_providers, get_provider


# ── device_flow callback registry ──────────────────────────────────────────

def test_prompt_callback_receives_emission():
    seen: list[DevicePrompt] = []
    set_prompt_callback(lambda p: seen.append(p))
    try:
        p = DevicePrompt(
            provider="azure", verification_uri="https://aka.ms/devicelogin",
            user_code="ABC123", expires_in=900, message="visit the URL",
        )
        emit_prompt(p)
        assert seen == [p]
    finally:
        set_prompt_callback(None)


def test_prompt_callback_silent_when_unset():
    set_prompt_callback(None)
    emit_prompt(DevicePrompt("p", "u", "c", 0, ""))  # must not raise


def test_prompt_callback_is_thread_local():
    """cb set in thread A must not bleed into thread B."""
    seen_a: list = []
    seen_b: list = []
    barrier = threading.Barrier(2)

    def a():
        set_prompt_callback(lambda p: seen_a.append(p))
        barrier.wait()
        # Let b emit before a does.
        time.sleep(0.05)
        emit_prompt(DevicePrompt("a-p", "u", "c", 0, ""))
        set_prompt_callback(None)

    def b():
        barrier.wait()
        # b has never set a callback — its emission must be a no-op.
        emit_prompt(DevicePrompt("b-p", "u", "c", 0, ""))

    ta = threading.Thread(target=a)
    tb = threading.Thread(target=b)
    ta.start(); tb.start()
    ta.join();  tb.join()

    # a saw only its own emission; b's emission went nowhere.
    assert [p.provider for p in seen_a] == ["a-p"]
    assert seen_b == []


# ── AzureDeviceProvider ────────────────────────────────────────────────────

def _mock_msal_app(*, initiate_result: dict, acquire_result: dict) -> MagicMock:
    app = MagicMock()
    app.initiate_device_flow.return_value = initiate_result
    app.acquire_token_by_device_flow.return_value = acquire_result
    return app


def test_azure_device_provider_happy_path():
    prompts: list[DevicePrompt] = []
    set_prompt_callback(lambda p: prompts.append(p))
    try:
        flow_dict = {
            "user_code": "ABC123",
            "device_code": "dev-code",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
            "message": "open the URL and enter ABC123",
        }
        token_result = {
            "access_token": "fake-access-token",
            "expires_in": 3600,
        }
        mocked_app = _mock_msal_app(initiate_result=flow_dict,
                                    acquire_result=token_result)

        p = AzureDeviceProvider(
            "azure-priv",
            tenant="t-id",
            client_id="c-id",
            scope="https://management.azure.com/.default",
            cache=TokenCache(),
        )
        p._app = mocked_app   # bypass lazy MSAL init

        rec = p._fetch("alice")
        assert rec.token == "fake-access-token"
        assert rec.expires_at > time.time() + 3000

        # Emission happened before the token arrived.
        assert len(prompts) == 1
        assert prompts[0].provider == "azure-priv"
        assert prompts[0].user_code == "ABC123"
    finally:
        set_prompt_callback(None)


def test_azure_device_provider_initiate_failure():
    p = AzureDeviceProvider(
        "azure-priv", tenant="t", client_id="c",
        scope="s", cache=TokenCache(),
    )
    p._app = _mock_msal_app(
        initiate_result={"error": "invalid_request", "error_description": "bad"},
        acquire_result={},
    )
    with pytest.raises(RuntimeError, match="device flow init failed"):
        p._fetch("alice")


def test_azure_device_provider_acquire_failure():
    p = AzureDeviceProvider(
        "azure-priv", tenant="t", client_id="c",
        scope="s", cache=TokenCache(),
    )
    p._app = _mock_msal_app(
        initiate_result={
            "user_code": "X", "device_code": "d",
            "verification_uri": "u", "expires_in": 60, "interval": 5, "message": "m",
        },
        acquire_result={"error": "expired_token", "error_description": "too late"},
    )
    set_prompt_callback(None)
    with pytest.raises(RuntimeError, match="device flow failed"):
        p._fetch("alice")


def test_azure_device_provider_requires_user_id():
    p = AzureDeviceProvider(
        "azure-priv", tenant="t", client_id="c", scope="s", cache=TokenCache(),
    )
    # Calling with None — Provider.get_valid_token in device_code mode
    # enforces this, but defensive check in _fetch too.
    with pytest.raises(RuntimeError):
        p._fetch(None)


# ── Registry dispatch ──────────────────────────────────────────────────────

def test_registry_builds_azure_device_provider():
    clear_providers()
    build_providers({
        "azure-priv": {
            "mode": "device_code",
            "type": "azure",
            "tenant": "t-id",
            "client_id": "c-id",
            "scope": "https://management.azure.com/.default",
        },
    })
    p = get_provider("azure-priv")
    assert isinstance(p, AzureDeviceProvider)
    assert p.mode == "device_code"
    assert p._tenant == "t-id"
    assert p._client_id == "c-id"


def test_registry_rejects_azure_device_without_tenant():
    clear_providers()
    with pytest.raises(ValueError, match="tenant and client_id"):
        build_providers({
            "x": {"mode": "device_code", "type": "azure", "client_id": "c"},
        })


def test_registry_rejects_unknown_device_type():
    clear_providers()
    with pytest.raises(ValueError, match="not yet supported"):
        build_providers({
            "sf": {"mode": "device_code", "type": "snowflake"},
        })
