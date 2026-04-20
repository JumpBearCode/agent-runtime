"""Shared pytest fixtures.

These tests cover agent_runtime in isolation — no real Anthropic API calls,
no real MCP servers. Anything that would hit the network is either skipped
or stubbed.

Required env (read from .env via core/config.py at import time):
    MODEL_ID
    ANTHROPIC_API_KEY (only validated lazily by SDK; not needed for these tests)
"""

from __future__ import annotations

import threading

import pytest

from agent_runtime.core import tools as tools_mod


@pytest.fixture(autouse=True)
def _reset_thread_state():
    """Clear per-thread state before/after each test so tests can't pollute each other."""
    tools_mod.set_thread_hooks(None)
    tools_mod.set_thread_todo(None)
    yield
    tools_mod.set_thread_hooks(None)
    tools_mod.set_thread_todo(None)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point config.WORKDIR at a fresh temp dir so file-IO tools stay sandboxed."""
    from agent_runtime.core import config
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    return tmp_path


@pytest.fixture
def api_client():
    """FastAPI TestClient with the real engine wired up via lifespan.

    Session-scoped would be faster, but per-test gives clean isolation
    (no leftover slots in _ConfirmRegistry, etc.).
    """
    from fastapi.testclient import TestClient
    from agent_runtime.api.app import app
    with TestClient(app) as c:
        yield c


def run_in_thread(fn, *args, **kwargs):
    """Run fn in a new thread, return its result. Used for thread-local tests."""
    box: dict = {}

    def _runner():
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as e:
            box["error"] = e

    t = threading.Thread(target=_runner)
    t.start()
    t.join(timeout=5)
    if "error" in box:
        raise box["error"]
    return box.get("result")
