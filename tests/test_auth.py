"""Auth — identity parsing, dev bypass, and the FastAPI dependency wiring.

JWT signature validation itself relies on Azure AD's live JWKS endpoint;
we don't exercise it here — instead we verify the dev-mode bypass and
the 401 paths that gate real requests when dev mode is off.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


# ── parse_easyauth_headers ──────────────────────────────────────────────────

def test_easyauth_dev_mode(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    from auth import easyauth
    importlib.reload(easyauth)  # pick up env change

    u = easyauth.parse_easyauth_headers({})
    assert u is not None
    assert u.is_authenticated
    assert u.user_id  # synthesized


def test_easyauth_no_headers(monkeypatch):
    monkeypatch.delenv("AUTH_DEV_MODE", raising=False)
    from auth import easyauth
    importlib.reload(easyauth)

    assert easyauth.parse_easyauth_headers({}) is None


def test_easyauth_extracts_oid_and_display_name(monkeypatch):
    import base64
    import json

    monkeypatch.delenv("AUTH_DEV_MODE", raising=False)
    from auth import easyauth
    importlib.reload(easyauth)

    principal = base64.b64encode(
        json.dumps({"claims": [
            {"typ": "name", "val": "Alice Example"},
            {"typ": "sub", "val": "ignored"},
        ]}).encode()
    ).decode()

    u = easyauth.parse_easyauth_headers({
        "X-MS-CLIENT-PRINCIPAL-ID":   "oid-abc",
        "X-MS-CLIENT-PRINCIPAL-NAME": "alice@example.com",
        "X-MS-CLIENT-PRINCIPAL":      principal,
        "X-MS-TOKEN-AAD-ID-TOKEN":    "raw.jwt.here",
    })
    assert u is not None
    assert u.user_id == "oid-abc"
    assert u.principal_name == "alice@example.com"
    assert u.display_name == "Alice Example"
    assert u.raw_token == "raw.jwt.here"


def test_easyauth_malformed_principal_degrades_gracefully(monkeypatch):
    monkeypatch.delenv("AUTH_DEV_MODE", raising=False)
    from auth import easyauth
    importlib.reload(easyauth)

    u = easyauth.parse_easyauth_headers({
        "X-MS-CLIENT-PRINCIPAL-ID": "oid-abc",
        "X-MS-CLIENT-PRINCIPAL":    "not-valid-base64!!!",
    })
    # Auth still succeeds (we have oid) but display_name is empty.
    assert u is not None
    assert u.user_id == "oid-abc"
    assert u.display_name == ""


# ── runtime dependency: dev mode bypass ─────────────────────────────────────

def test_runtime_chat_accepts_request_in_dev_mode(api_client):
    """In AUTH_DEV_MODE the dependency short-circuits, no Authorization needed.
    We hit validation failures (empty messages) AFTER auth passes."""
    r = api_client.post("/api/chat", json={"messages": []})
    assert r.status_code == 400  # validation, not 401


# ── runtime dependency: prod-mode 401s ──────────────────────────────────────

@pytest.fixture
def prod_auth_client(monkeypatch):
    """Reload config with AUTH_DEV_MODE off so /api/chat demands a Bearer."""
    monkeypatch.delenv("AUTH_DEV_MODE", raising=False)
    monkeypatch.setenv("AAD_TENANT_ID", "tenant-for-test")
    monkeypatch.setenv("AAD_AUDIENCE", "api://test")

    from agent_runtime.core import config as config_mod
    from agent_runtime.api import deps as deps_mod
    from agent_runtime.api.routes import chat as chat_mod
    from agent_runtime.api.routes import confirm as confirm_mod
    from agent_runtime.api import app as app_mod

    importlib.reload(config_mod)
    importlib.reload(deps_mod)
    importlib.reload(chat_mod)
    importlib.reload(confirm_mod)
    importlib.reload(app_mod)

    with TestClient(app_mod.app) as c:
        yield c

    # Restore dev mode for the rest of the session.
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    importlib.reload(config_mod)
    importlib.reload(deps_mod)
    importlib.reload(chat_mod)
    importlib.reload(confirm_mod)
    importlib.reload(app_mod)


def test_runtime_chat_rejects_missing_bearer(prod_auth_client):
    r = prod_auth_client.post("/api/chat", json={"messages": [
        {"role": "user", "content": "hi"},
    ]})
    assert r.status_code == 401
    assert "bearer" in r.json()["detail"].lower()


def test_runtime_chat_rejects_bogus_bearer(prod_auth_client):
    r = prod_auth_client.post(
        "/api/chat",
        headers={"Authorization": "Bearer not-a-real-jwt"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


def test_runtime_healthz_stays_open(prod_auth_client):
    """Health check must not require auth — used by Azure probes."""
    r = prod_auth_client.get("/api/healthz")
    assert r.status_code == 200
