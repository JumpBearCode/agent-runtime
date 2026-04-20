"""FastAPI routes — meta + chat validation + confirm.

Real engine, real lifespan, no LLM calls. The /chat happy path needs
Anthropic and is exercised via smoke tests outside pytest; here we only
verify validation, status codes, and shapes.
"""

import pytest

from agent_runtime.core import config


# ── meta ──────────────────────────────────────────────────────────────────

def test_healthz(api_client):
    r = api_client.get("/api/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_info_shape(api_client):
    r = api_client.get("/api/info")
    assert r.status_code == 200
    body = r.json()
    # Exactly 5 documented fields, nothing else.
    assert set(body.keys()) == {"agent_name", "model", "mcp_tools", "hitl_tools", "hitl_timeout"}
    assert body["model"] == config.MODEL
    assert body["hitl_timeout"] == config.HITL_TIMEOUT
    assert isinstance(body["mcp_tools"], list)
    assert isinstance(body["hitl_tools"], list)


def test_info_does_not_leak_internal_paths(api_client):
    body = api_client.get("/api/info").json()
    # Internal paths used to be exposed pre-#11 — make sure they stay gone.
    for leaked in ("workspace", "settings_dir", "skills_dir", "system_prompt_file"):
        assert leaked not in body


def test_tools_returns_builtin_set(api_client):
    tools = api_client.get("/api/tools").json()
    assert isinstance(tools, list)
    expected = {"bash", "read_file", "write_file", "edit_file",
                "todo_write", "todo_read", "load_skill"}
    assert expected.issubset(set(tools))
    # `compact` was removed when we ripped out auto_compact.
    assert "compact" not in tools


def test_skills_returns_dict(api_client):
    r = api_client.get("/api/skills")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_skill_content_404_for_unknown(api_client):
    r = api_client.get("/api/skills/never-defined-skill")
    assert r.status_code == 404
    assert "unknown skill" in r.json()["detail"].lower()


# ── chat: validation only (no LLM) ─────────────────────────────────────────

def test_chat_rejects_missing_messages(api_client):
    r = api_client.post("/api/chat", json={})
    assert r.status_code == 400
    assert "messages" in r.json()["detail"]


def test_chat_rejects_empty_messages(api_client):
    r = api_client.post("/api/chat", json={"messages": []})
    assert r.status_code == 400


def test_chat_rejects_non_array_messages(api_client):
    r = api_client.post("/api/chat", json={"messages": "hello"})
    assert r.status_code == 400


def test_chat_rejects_assistant_as_last_message(api_client):
    r = api_client.post("/api/chat", json={
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hi back"},
        ]
    })
    assert r.status_code == 400
    assert "user" in r.json()["detail"].lower()


# ── confirm ────────────────────────────────────────────────────────────────

def test_confirm_unknown_request_returns_410(api_client):
    r = api_client.post("/api/confirm/never-existed-id", json={"allowed": True})
    assert r.status_code == 410
    assert "no longer pending" in r.json()["detail"]


def test_confirm_missing_body_defaults_to_deny(api_client):
    """Empty body → allowed defaults to False — but slot still doesn't exist → 410."""
    r = api_client.post("/api/confirm/never", json={})
    assert r.status_code == 410


def test_confirm_resolves_a_real_slot(api_client):
    """Open a slot via the engine directly, then resolve it through the route."""
    engine = api_client.app.state.engine
    req_id, slot = engine._confirm_registry.open("trace-T", "bash")
    r = api_client.post(f"/api/confirm/{req_id}", json={"allowed": True})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "allowed": True}
    assert slot.event.is_set()
    assert slot.result is True


# ── CORS ──────────────────────────────────────────────────────────────────

def test_cors_headers_present(api_client):
    r = api_client.options(
        "/api/healthz",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in {k.lower() for k in r.headers}
