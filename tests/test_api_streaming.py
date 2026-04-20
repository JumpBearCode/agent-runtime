"""End-to-end SSE tests for POST /api/chat — driven by FakeAnthropicClient.

These exercise the full request lifecycle without hitting the real Anthropic
API: agent_loop → _stream_response → on_event → asyncio.Queue → SSE → wire.

Two flavors:
  * Non-HITL tests use the sync `TestClient` (simpler, single request).
  * HITL tests spawn a real uvicorn on a free port. ASGITransport buffers
    enough that interleaving an SSE read with a /confirm POST against the
    same in-process app is unreliable — a real socket fixes this.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from .conftest import (
    FakeAnthropicClient,
    StatefulFakeClient,
    parse_sse_lines,
    text_round,
    tool_use_round,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_app(monkeypatch, *, hitl_tools: list[str] = None,
              skills_dir=None, hitl_timeout: float = None):
    """Build a fresh FastAPI app whose engine has the given HITL/skills config.

    Must monkeypatch BEFORE importing app.app, because lifespan reads config
    once at engine __init__.
    """
    from agent_runtime.core import config
    if hitl_tools is not None:
        # Bypass file resolution by monkeypatching resolve_hitl directly.
        monkeypatch.setattr(config, "resolve_hitl",
                            lambda: set(hitl_tools), raising=True)
    if skills_dir is not None:
        monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)
    if hitl_timeout is not None:
        monkeypatch.setattr(config, "HITL_TIMEOUT", hitl_timeout)

    from agent_runtime.api.app import app as _app
    return _app


def _post_chat_sse(client, messages, trace_id="t-test"):
    """Open SSE and return an iterator of parsed events."""
    response_ctx = client.stream(
        "POST", "/api/chat",
        json={"messages": messages, "trace_id": trace_id},
    )
    return response_ctx


def _drain(events_iter, timeout: float = 5.0):
    """Collect all SSE events into a list, with overall timeout."""
    deadline = time.time() + timeout
    out = []
    for evt in events_iter:
        out.append(evt)
        if time.time() > deadline:
            raise TimeoutError("SSE stream did not finish in time")
        if evt.get("event") == "done":
            # 'done' doesn't always end the byte stream; keep reading
            # but bail early after a couple more iters via the next loop.
            pass
    return out


# ── Happy path: text-only round ────────────────────────────────────────────

def test_chat_streams_text_only_round(monkeypatch):
    from agent_runtime.core import config
    monkeypatch.setattr(config, "client", FakeAnthropicClient([
        text_round("hello world", stop_reason="end_turn"),
    ]))
    app = _make_app(monkeypatch)
    with TestClient(app) as client:
        with _post_chat_sse(client, [{"role": "user", "content": "hi"}]) as r:
            assert r.status_code == 200
            events = list(parse_sse_lines(r.iter_lines()))

    types = [e["event"] for e in events]
    assert "text_delta" in types
    assert "text_stop" in types
    # Contract: exactly one `done` per stream — it terminates the SSE.
    assert types.count("done") == 1, f"expected exactly one done; types={types}"
    text_evt = next(e for e in events if e["event"] == "text_delta")
    assert text_evt["data"]["text"] == "hello world"
    done_evt = next(e for e in events if e["event"] == "done")
    assert done_evt["data"]["stop_reason"] == "end_turn"


# ── Multi-round: tool execution ────────────────────────────────────────────

def test_chat_runs_tool_then_completes(monkeypatch, tmp_path):
    """LLM emits tool_use(write_file), agent runs it, second LLM call ends."""
    from agent_runtime.core import config
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "client", FakeAnthropicClient([
        tool_use_round("write_file",
                       {"path": "out.txt", "content": "from-tool"}),
        text_round("done", stop_reason="end_turn"),
    ]))
    app = _make_app(monkeypatch)
    with TestClient(app) as client:
        with _post_chat_sse(client, [{"role": "user", "content": "write it"}]) as r:
            events = list(parse_sse_lines(r.iter_lines()))

    types = [e["event"] for e in events]
    assert types.count("tool_call") == 1
    assert types.count("tool_result") == 1
    assert "done" in types

    # The tool actually ran — file should exist.
    assert (tmp_path / "out.txt").read_text() == "from-tool"

    # tool_result event payload should reflect success, not error.
    tr = next(e for e in events if e["event"] == "tool_result")
    assert tr["data"]["is_error"] is False
    assert "Wrote" in tr["data"]["output"]


# ── HITL helpers: spawn real uvicorn on a free port ──────────────────────

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def live_server(app, *, timeout: float = 5.0):
    """Run the given FastAPI app under uvicorn in a background thread.

    Yields the base URL. Triggers full lifespan on startup; signals exit on
    teardown and joins the thread.
    """
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", loop="asyncio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + timeout
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        raise RuntimeError("uvicorn did not start in time")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


import time  # noqa: E402  (used by live_server above)


async def _stream_chat_capture(base_url: str, *, json_body: dict,
                                until_event: str | None = None,
                                timeout: float = 10.0):
    """Open SSE on a real port, collect events. If until_event is set,
    return as soon as that event is seen (caller takes over). Otherwise
    drain the whole stream.

    Returns (captured_list, seen_event_or_None, reader_task_or_None).
    When until_event is set, caller is responsible for awaiting reader_task
    after taking whatever action they need.
    """
    captured: list[dict] = []

    async def _drain_into(reader_task_holder=None):
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            async with client.stream("POST", "/api/chat", json=json_body) as r:
                buf: list[str] = []
                async for raw in r.aiter_lines():
                    buf.append(raw)
                    if raw == "":
                        for evt in parse_sse_lines(buf):
                            captured.append(evt)
                        buf = []

    if until_event is None:
        await _drain_into()
        return captured, None, None

    seen = asyncio.Event()
    seen_payload: dict = {}

    async def _drain_with_signal():
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            async with client.stream("POST", "/api/chat", json=json_body) as r:
                buf: list[str] = []
                async for raw in r.aiter_lines():
                    buf.append(raw)
                    if raw == "":
                        for evt in parse_sse_lines(buf):
                            captured.append(evt)
                            if evt.get("event") == until_event and not seen.is_set():
                                seen_payload.update(evt)
                                seen.set()
                        buf = []

    task = asyncio.create_task(_drain_with_signal())
    try:
        await asyncio.wait_for(seen.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        raise AssertionError(
            f"timed out waiting for event '{until_event}'; captured={captured}"
        )
    return captured, seen_payload, task


# ── HITL: user allows ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_chat_with_hitl_user_allows(monkeypatch, tmp_path):
    """Tool gated by HITL → SSE emits confirm_request → POST /api/confirm
    → server resumes, runs tool, completes."""
    from agent_runtime.core import config
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "client", FakeAnthropicClient([
        tool_use_round("write_file",
                       {"path": "x.txt", "content": "ok"}, tool_id="tu_a"),
        text_round("finished", stop_reason="end_turn"),
    ]))
    app = _make_app(monkeypatch, hitl_tools=["write_file"])

    with live_server(app) as base_url:
        captured, seen, reader_task = await _stream_chat_capture(
            base_url, until_event="confirm_request",
            json_body={"messages": [{"role": "user", "content": "do"}]},
        )
        request_id = seen["data"]["request_id"]

        async with httpx.AsyncClient(base_url=base_url) as confirm_client:
            r = await confirm_client.post(
                f"/api/confirm/{request_id}", json={"allowed": True})
            assert r.status_code == 200

        await asyncio.wait_for(reader_task, timeout=5.0)

    types = [e["event"] for e in captured]
    assert "confirm_request" in types
    assert "tool_result" in types
    assert "done" in types
    assert (tmp_path / "x.txt").read_text() == "ok"


# ── HITL: timeout ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_chat_with_hitl_timeout_aborts(monkeypatch, tmp_path):
    """No /confirm POST → after AGENT_HITL_TIMEOUT the round aborts cleanly,
    done event has stop_reason=hitl_timeout, file is NOT written."""
    from agent_runtime.core import config
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "client", FakeAnthropicClient([
        tool_use_round("write_file",
                       {"path": "blocked.txt", "content": "x"}),
    ]))
    app = _make_app(monkeypatch, hitl_tools=["write_file"], hitl_timeout=0.3)

    with live_server(app) as base_url:
        events, _, _ = await _stream_chat_capture(
            base_url, json_body={"messages": [{"role": "user", "content": "go"}]},
        )

    done_evts = [e for e in events if e["event"] == "done"]
    assert any(d["data"].get("stop_reason") == "hitl_timeout" for d in done_evts), \
        f"no hitl_timeout done; types={[e['event'] for e in events]}"

    trs = [e for e in events if e["event"] == "tool_result"]
    assert len(trs) == 1
    assert "timeout" in trs[0]["data"]["output"].lower()

    assert not (tmp_path / "blocked.txt").exists()


# ── HITL: user explicitly denies (round continues) ─────────────────────────

@pytest.mark.anyio
async def test_chat_with_hitl_user_denies_continues_round(monkeypatch, tmp_path):
    """User clicks Deny → tool_result is 'Blocked: User rejected' →
    LLM gets a chance to react in the next round."""
    from agent_runtime.core import config
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "client", FakeAnthropicClient([
        tool_use_round("write_file",
                       {"path": "n.txt", "content": "bad"}),
        text_round("understood, won't write", stop_reason="end_turn"),
    ]))
    app = _make_app(monkeypatch, hitl_tools=["write_file"])

    with live_server(app) as base_url:
        captured, seen, reader_task = await _stream_chat_capture(
            base_url, until_event="confirm_request",
            json_body={"messages": [{"role": "user", "content": "do"}]},
        )
        request_id = seen["data"]["request_id"]

        async with httpx.AsyncClient(base_url=base_url) as confirm_client:
            r = await confirm_client.post(
                f"/api/confirm/{request_id}", json={"allowed": False})
            assert r.status_code == 200

        await asyncio.wait_for(reader_task, timeout=5.0)

    text_evts = [e for e in captured if e["event"] == "text_delta"]
    assert any("understood" in e["data"]["text"] for e in text_evts)

    done = next(e for e in captured if e["event"] == "done")
    assert done["data"]["stop_reason"] == "end_turn"

    assert not (tmp_path / "n.txt").exists()

    tr = next(e for e in captured if e["event"] == "tool_result")
    assert "rejected" in tr["data"]["output"].lower()


# ── HITL: two concurrent chats, no cross-routing ──────────────────────────

@pytest.mark.anyio
async def test_concurrent_hitl_chats_no_cross_routing(monkeypatch, tmp_path):
    """Two chats hit HITL simultaneously. Each must receive its own
    confirm_request (different request_ids), and resolving them in any
    order must complete only the matching chat. End-to-end regression for
    the _on_event cross-chat overwrite bug.
    """
    from agent_runtime.core import config
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "client", StatefulFakeClient(
        tool_name="bash", tool_args={"command": "echo x"},
        response_text="all done",
    ))
    app = _make_app(monkeypatch, hitl_tools=["bash"])

    with live_server(app) as base_url:
        # Stage A: open chat A's stream and wait for its confirm_request.
        cap_A, seen_A, reader_A = await _stream_chat_capture(
            base_url, until_event="confirm_request",
            json_body={"messages": [{"role": "user", "content": "do A"}],
                       "trace_id": "trace-A"},
        )
        # Stage B: open chat B's stream — server must accept it while A
        # is still blocked in HITL.
        cap_B, seen_B, reader_B = await _stream_chat_capture(
            base_url, until_event="confirm_request",
            json_body={"messages": [{"role": "user", "content": "do B"}],
                       "trace_id": "trace-B"},
        )

        req_A = seen_A["data"]["request_id"]
        req_B = seen_B["data"]["request_id"]
        assert req_A != req_B, "concurrent chats produced colliding request_ids"

        # Resolve in REVERSE order: B first, then A. If routing were broken
        # this would (a) fail to find the slot, or (b) wake the wrong agent.
        async with httpx.AsyncClient(base_url=base_url) as confirm:
            r = await confirm.post(f"/api/confirm/{req_B}", json={"allowed": True})
            assert r.status_code == 200
            r = await confirm.post(f"/api/confirm/{req_A}", json={"allowed": True})
            assert r.status_code == 200

        await asyncio.wait_for(reader_A, timeout=5.0)
        await asyncio.wait_for(reader_B, timeout=5.0)

    # Both chats independently completed with end_turn.
    done_A = next(e for e in cap_A if e["event"] == "done")
    done_B = next(e for e in cap_B if e["event"] == "done")
    assert done_A["data"]["stop_reason"] == "end_turn"
    assert done_B["data"]["stop_reason"] == "end_turn"

    # Each chat's confirm_request carried only its own confirm_request id.
    cr_A = [e for e in cap_A if e["event"] == "confirm_request"]
    cr_B = [e for e in cap_B if e["event"] == "confirm_request"]
    assert len(cr_A) == 1 and cr_A[0]["data"]["request_id"] == req_A
    assert len(cr_B) == 1 and cr_B[0]["data"]["request_id"] == req_B

    # Sanity: each chat saw exactly one done (terminator contract).
    assert sum(1 for e in cap_A if e["event"] == "done") == 1
    assert sum(1 for e in cap_B if e["event"] == "done") == 1


# ── HITL: SSE disconnect releases the slot ─────────────────────────────────

@pytest.mark.anyio
async def test_sse_disconnect_during_hitl_releases_slot(monkeypatch, tmp_path):
    """Client closes SSE while in HITL → server's chat_stream catches
    asyncio.CancelledError → cancel_trace fires → slot is gone. Subsequent
    POST /api/confirm/{request_id} returns 410. End-to-end regression for
    the cancel_trace cleanup path.
    """
    from agent_runtime.core import config
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "client", StatefulFakeClient(
        tool_name="bash", tool_args={"command": "echo x"},
    ))
    app = _make_app(monkeypatch, hitl_tools=["bash"])

    with live_server(app) as base_url:
        request_id: str | None = None

        # Open SSE, read until we see confirm_request, then exit the
        # context to force-close the connection.
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            async with client.stream("POST", "/api/chat", json={
                "messages": [{"role": "user", "content": "do"}],
                "trace_id": "trace-disc",
            }) as r:
                buf: list[str] = []
                async for raw in r.aiter_lines():
                    buf.append(raw)
                    if raw == "":
                        for evt in parse_sse_lines(buf):
                            if evt.get("event") == "confirm_request":
                                request_id = evt["data"]["request_id"]
                                break
                        buf = []
                        if request_id:
                            break  # exit the stream context → close connection

        assert request_id, "never saw confirm_request"

        # Give uvicorn + chat_stream a moment to react to the disconnect.
        await asyncio.sleep(0.3)

        # Slot must be gone — POST /confirm with the now-cancelled id is 410.
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.post(f"/api/confirm/{request_id}",
                                   json={"allowed": True})
            assert r.status_code == 410, \
                f"expected 410 (slot cancelled), got {r.status_code}: {r.text}"


# ── /api/skills/{name} positive case ───────────────────────────────────────

def test_skill_content_returns_body(monkeypatch, tmp_path):
    """Plant a SKILL.md and verify GET /api/skills/{name} returns its body."""
    skills_dir = tmp_path / "skills"
    sd = skills_dir / "my-skill"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: a test skill\n---\nThe body here."
    )
    app = _make_app(monkeypatch, skills_dir=skills_dir)
    with TestClient(app) as client:
        r = client.get("/api/skills/my-skill")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "my-skill"
        assert "The body here." in body["content"]

        listing = client.get("/api/skills").json()
        assert listing["my-skill"] == "a test skill"
