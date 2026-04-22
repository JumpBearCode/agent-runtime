"""auth.config + auth.middleware + tools-layer auth injection."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from auth.cache import TokenCache, TokenRecord
from auth.config import AuthConfig, load_auth_config
from auth.middleware import inject_auth_for_mcp
from auth.providers.base import Provider, ProviderMode
from auth.providers.registry import build_providers, clear_providers


# ── config loader ──────────────────────────────────────────────────────────

def test_load_auth_config_missing_file(tmp_path: Path):
    cfg = load_auth_config(tmp_path / "nope.json")
    assert cfg.providers == {}
    assert cfg.mcp_bindings == {}
    assert cfg.runtime == {}


def test_load_auth_config_happy_path(tmp_path: Path):
    (tmp_path / "auth.json").write_text(json.dumps({
        "runtime": {"audience": "api://r", "required_scope": "runtime.access"},
        "providers": {
            "azure": {"mode": "service", "scope": "https://management.azure.com/.default"},
        },
        "mcp_bindings": {"adf": "azure"},
    }))
    cfg = load_auth_config(tmp_path / "auth.json")
    assert cfg.providers["azure"]["mode"] == "service"
    assert cfg.mcp_bindings == {"adf": "azure"}
    assert cfg.provider_for_mcp("adf") == "azure"
    assert cfg.provider_for_mcp("nonexistent") is None


def test_load_auth_config_rejects_binding_to_unknown_provider(tmp_path: Path):
    (tmp_path / "auth.json").write_text(json.dumps({
        "providers": {"azure": {"mode": "service"}},
        "mcp_bindings": {"adf": "nonexistent_provider"},
    }))
    with pytest.raises(ValueError, match="no such provider"):
        load_auth_config(tmp_path / "auth.json")


# ── middleware ─────────────────────────────────────────────────────────────

class _StubServiceProvider(Provider):
    mode: ProviderMode = "service"

    def _fetch(self, user_id):
        return TokenRecord(token="stub-token", expires_at=time.time() + 600)


def test_inject_auth_returns_empty_for_unbound_mcp():
    clear_providers()
    cfg = AuthConfig()
    assert inject_auth_for_mcp(cfg, "anything") == {}


def test_inject_auth_returns_kwargs_for_bound_mcp(monkeypatch):
    clear_providers()
    # Register our stub via the registry's internal dict.
    from auth.providers import registry
    provider = _StubServiceProvider("azure", cache=TokenCache())
    registry._providers = {"azure": provider}

    cfg = AuthConfig(
        providers={"azure": {"mode": "service"}},
        mcp_bindings={"adf": "azure"},
    )
    kwargs = inject_auth_for_mcp(cfg, "adf")
    assert kwargs["_auth_token"] == "stub-token"
    assert kwargs["_auth_expires_at"] > 0


def test_inject_auth_unknown_provider_raises_keyerror():
    clear_providers()
    cfg = AuthConfig(
        providers={"azure": {"mode": "service"}},
        mcp_bindings={"adf": "azure"},
    )
    with pytest.raises(KeyError):
        inject_auth_for_mcp(cfg, "adf")


# ── tools.py integration: MCP dispatch gets auth kwargs merged ─────────────

def test_tools_dispatch_merges_auth_kwargs_into_mcp_call(monkeypatch):
    """When an MCP server is bound to a provider, dispatch must add
    _auth_token and _auth_expires_at to the args handed to MCP.call_tool."""
    from agent_runtime.core import tools as tools_mod

    clear_providers()
    from auth.providers import registry
    provider = _StubServiceProvider("azure", cache=TokenCache())
    registry._providers = {"azure": provider}

    # Minimal fake MCPManager: owns one tool under server "adf".
    class _FakeMCP:
        tool_names = {"mcp_adf_list_pipelines"}

        def __init__(self):
            self.calls = []

        def server_for_tool(self, name):
            return "adf" if name in self.tool_names else None

        def call_tool(self, name, args):
            self.calls.append((name, args))
            return "ok"

    fake = _FakeMCP()
    monkeypatch.setattr(tools_mod, "MCP", fake)
    monkeypatch.setattr(tools_mod, "AUTH_CONFIG", AuthConfig(
        providers={"azure": {"mode": "service"}},
        mcp_bindings={"adf": "azure"},
    ))

    result = tools_mod.dispatch_tool("mcp_adf_list_pipelines", {"foo": "bar"})
    assert result == "ok"
    assert len(fake.calls) == 1
    name, args = fake.calls[0]
    assert name == "mcp_adf_list_pipelines"
    assert args["foo"] == "bar"
    assert args["_auth_token"] == "stub-token"
    assert isinstance(args["_auth_expires_at"], int)


def test_tools_dispatch_leaves_builtin_tools_alone(monkeypatch):
    """Built-in tools (bash, read_file, ...) don't go through MCP and
    must NOT have _auth_token injected — the dispatch path is different."""
    from agent_runtime.core import tools as tools_mod

    clear_providers()
    # Auth config present but shouldn't matter for built-ins.
    monkeypatch.setattr(tools_mod, "AUTH_CONFIG", AuthConfig())

    result = tools_mod.dispatch_tool("bash", {"command": "echo hi"})
    assert "hi" in result
