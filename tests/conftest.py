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


@pytest.fixture
def anyio_backend():
    """Force pytest-anyio to use asyncio only (skip the trio leg)."""
    return "asyncio"


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


# ── Fake Anthropic client for streaming tests ─────────────────────────────
#
# The agent loop calls config.client.messages.stream(**kwargs) which returns
# a context manager that:
#   • is iterable, yielding events with .type and .delta / .content_block
#   • has get_final_message() returning a Message with .content + .usage
#
# We mimic just enough of that surface to drive agent_loop deterministically.

from types import SimpleNamespace
import json as _json


class _FakeStream:
    """Mimics anthropic.MessageStream context manager."""

    def __init__(self, events, final_message):
        self._events = events
        self._final = final_message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


class FakeAnthropicClient:
    """Drop-in replacement for `config.client` driven by a scripted list of
    (events, final_message) tuples — one per LLM round.
    """

    def __init__(self, scripted_rounds):
        self._scripted = list(scripted_rounds)
        self.calls = 0  # how many stream() calls were made (for assertions)

        class _Messages:
            def __init__(self, outer):
                self._outer = outer

            def stream(self, **kwargs):
                self._outer.calls += 1
                if not self._outer._scripted:
                    raise RuntimeError("FakeAnthropicClient: no more scripted rounds")
                events, final = self._outer._scripted.pop(0)
                return _FakeStream(events, final)

            def create(self, **kwargs):
                # Compaction would call this — agent_loop after #3 doesn't.
                raise RuntimeError("FakeAnthropicClient: messages.create unexpected")

        self.messages = _Messages(self)


def _usage(input_tokens=10, output_tokens=2):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def text_round(text: str, stop_reason: str = "end_turn"):
    """Single-block text response. agent_loop will set stop_reason then return."""
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=_usage(),
    )
    events = [
        SimpleNamespace(type="content_block_start",
                        content_block=SimpleNamespace(type="text")),
        SimpleNamespace(type="content_block_delta",
                        delta=SimpleNamespace(type="text_delta", text=text)),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="message_delta",
                        delta=SimpleNamespace(stop_reason=stop_reason)),
    ]
    return events, final


class StatefulFakeClient:
    """Like FakeAnthropicClient but reactive — picks the response based on
    the request payload's message count instead of a shared script position.

    Behaviour:
      * messages length == 1 (initial user only) → return tool_use
      * messages length > 1  (history with tool_result) → return text

    This makes it safe for *concurrent* chats: each call's reply depends only
    on its own messages payload, not on a counter shared across chats. NOTE:
    we use the count and not last-message content because agent_loop's
    _stream_response mutates the last user message in-place (wraps the
    string in a text block with cache_control before calling .stream).
    """

    def __init__(self, tool_name: str = "bash",
                 tool_args: dict | None = None,
                 response_text: str = "done"):
        self.tool_name = tool_name
        self.tool_args = tool_args or {"command": "echo hi"}
        self.response_text = response_text
        self.calls = 0
        self._tool_id_counter = 0
        self._counter_lock = threading.Lock()

        outer = self

        class _Messages:
            def stream(self, **kwargs):
                outer.calls += 1
                msgs = kwargs.get("messages", [])
                if len(msgs) <= 1:
                    with outer._counter_lock:
                        outer._tool_id_counter += 1
                        tid = outer._tool_id_counter
                    events, final = tool_use_round(
                        outer.tool_name, outer.tool_args, tool_id=f"tu_{tid}",
                    )
                else:
                    events, final = text_round(outer.response_text)
                return _FakeStream(events, final)

            def create(self, **kwargs):
                raise RuntimeError("StatefulFakeClient: messages.create unexpected")

        self.messages = _Messages()


def tool_use_round(name: str, args: dict, tool_id: str = "tu_1"):
    """Single tool_use response. agent_loop will execute it then loop back."""
    final = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)],
        usage=_usage(),
    )
    events = [
        SimpleNamespace(type="content_block_start",
                        content_block=SimpleNamespace(
                            type="tool_use", id=tool_id, name=name)),
        SimpleNamespace(type="content_block_delta",
                        delta=SimpleNamespace(type="input_json_delta",
                                              partial_json=_json.dumps(args))),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="message_delta",
                        delta=SimpleNamespace(stop_reason="tool_use")),
    ]
    return events, final


# ── Minimal SSE parser ────────────────────────────────────────────────────

def parse_sse_lines(lines):
    """Iterate decoded lines from an SSE response and yield {event, data} dicts.

    `data` is JSON-decoded if possible. Blank line ends one event.
    """
    current: dict = {}
    for raw in lines:
        line = raw.strip() if isinstance(raw, str) else raw.decode().strip()
        if not line:
            if current:
                yield current
                current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            payload = line.split(":", 1)[1].strip()
            try:
                current["data"] = _json.loads(payload)
            except _json.JSONDecodeError:
                current["data"] = payload
    if current:
        yield current
