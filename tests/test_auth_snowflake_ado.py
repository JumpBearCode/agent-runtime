"""Snowflake + ADO providers. MSAL mocked — same pattern as the Azure
device flow tests. The goal is to confirm that adding a provider is a
small delta on top of the shared _aad_device helper.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from auth.cache import TokenCache
from auth.device_flow import set_prompt_callback
from auth.providers.ado_device import AdoDeviceProvider
from auth.providers.snowflake_device import SnowflakeDeviceProvider
from auth.providers.registry import build_providers, clear_providers, get_provider


def _mock_flow_app(user_code="X", access_token="tok"):
    app = MagicMock()
    app.initiate_device_flow.return_value = {
        "user_code": user_code,
        "device_code": "dc",
        "verification_uri": "https://microsoft.com/devicelogin",
        "expires_in": 900,
        "interval": 5,
        "message": "m",
    }
    app.acquire_token_by_device_flow.return_value = {
        "access_token": access_token, "expires_in": 3600,
    }
    return app


# ── Snowflake ──────────────────────────────────────────────────────────────

def test_snowflake_provider_fetch_returns_token():
    set_prompt_callback(None)
    p = SnowflakeDeviceProvider(
        "sf", tenant="t", client_id="c",
        scope="api://snowflake/session:scope:ANALYST",
        account="xy12345.us-east-1",
        cache=TokenCache(),
    )
    p._app = _mock_flow_app(access_token="sf-token")

    rec = p._fetch("alice")
    assert rec.token == "sf-token"
    # Provider exposes the Snowflake account for downstream connectors.
    assert p.account == "xy12345.us-east-1"


def test_registry_builds_snowflake_provider():
    clear_providers()
    build_providers({
        "snowflake": {
            "mode": "device_code",
            "type": "snowflake",
            "tenant": "t",
            "client_id": "c",
            "scope": "api://snowflake/session:scope:ANALYST",
            "account": "xy12345.us-east-1",
        },
    })
    p = get_provider("snowflake")
    assert isinstance(p, SnowflakeDeviceProvider)
    assert p.account == "xy12345.us-east-1"


def test_registry_rejects_snowflake_missing_account():
    clear_providers()
    with pytest.raises(ValueError, match="scope.*account"):
        build_providers({
            "snowflake": {
                "mode": "device_code", "type": "snowflake",
                "tenant": "t", "client_id": "c",
                "scope": "api://x/s:scope:r",
                # account missing
            },
        })


# ── ADO ─────────────────────────────────────────────────────────────────────

def test_ado_provider_fetch_returns_token():
    set_prompt_callback(None)
    p = AdoDeviceProvider(
        "ado", tenant="t", client_id="c", org="mycompany",
        cache=TokenCache(),
    )
    p._app = _mock_flow_app(access_token="ado-token")

    rec = p._fetch("alice")
    assert rec.token == "ado-token"
    assert p.org == "mycompany"


def test_ado_provider_uses_default_scope():
    p = AdoDeviceProvider(
        "ado", tenant="t", client_id="c", org="mycompany", cache=TokenCache(),
    )
    # Well-known Azure DevOps API resource ID.
    assert "499b84ac-1321-427f-aa17-267ca6975798" in p._scope


def test_registry_builds_ado_provider():
    clear_providers()
    build_providers({
        "ado": {
            "mode": "device_code",
            "type": "ado",
            "tenant": "t", "client_id": "c",
            "org": "mycompany",
        },
    })
    p = get_provider("ado")
    assert isinstance(p, AdoDeviceProvider)
    assert p.org == "mycompany"


def test_registry_rejects_ado_missing_org():
    clear_providers()
    with pytest.raises(ValueError, match="org"):
        build_providers({
            "ado": {
                "mode": "device_code", "type": "ado",
                "tenant": "t", "client_id": "c",
                # org missing
            },
        })


# ── Heterogeneous config ───────────────────────────────────────────────────

def test_registry_builds_all_four_provider_types_together():
    """An agent could realistically mix service-Azure, device-Azure,
    Snowflake, and ADO in one auth.json."""
    clear_providers()
    built = build_providers({
        "azure": {
            "mode": "service",
            "scope": "https://management.azure.com/.default",
        },
        "azure-priv": {
            "mode": "device_code", "type": "azure",
            "tenant": "t", "client_id": "c",
            "scope": "https://management.azure.com/.default",
        },
        "snowflake": {
            "mode": "device_code", "type": "snowflake",
            "tenant": "t", "client_id": "c",
            "scope": "api://snowflake/session:scope:ANALYST",
            "account": "xy12345",
        },
        "ado": {
            "mode": "device_code", "type": "ado",
            "tenant": "t", "client_id": "c",
            "org": "mycompany",
        },
    })
    assert set(built.keys()) == {"azure", "azure-priv", "snowflake", "ado"}
    assert built["azure"].mode == "service"
    assert built["azure-priv"].mode == "device_code"
    assert built["snowflake"].mode == "device_code"
    assert built["ado"].mode == "device_code"
