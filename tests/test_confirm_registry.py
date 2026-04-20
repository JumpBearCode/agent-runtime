"""_ConfirmRegistry + _RegistryConfirmHook — concurrency + lifecycle.

These cover the bug class that bit us repeatedly: cross-chat HITL routing.
The registry is the single shared piece of state; everything else is per-trace.
"""

import threading
import time

import pytest

from agent_runtime.core import config
from agent_runtime.core.hooks import AbortRound, HookResult
from agent_runtime.engine import (
    ConfirmSlot,
    _ConfirmRegistry,
    _RegistryConfirmHook,
)


# ── _ConfirmRegistry ────────────────────────────────────────────────────────

def test_open_returns_unique_req_ids():
    reg = _ConfirmRegistry()
    id1, _ = reg.open("trace-A", "bash")
    id2, _ = reg.open("trace-A", "bash")
    assert id1 != id2


def test_open_creates_slot_with_correct_metadata():
    reg = _ConfirmRegistry()
    req_id, slot = reg.open("trace-X", "write_file")
    assert slot.trace_id == "trace-X"
    assert slot.tool_name == "write_file"
    assert slot.result is None
    assert not slot.event.is_set()


def test_resolve_sets_result_and_unblocks():
    reg = _ConfirmRegistry()
    req_id, slot = reg.open("t", "bash")
    assert reg.resolve(req_id, True)
    assert slot.result is True
    assert slot.event.is_set()


def test_resolve_unknown_returns_false():
    reg = _ConfirmRegistry()
    assert not reg.resolve("never-existed", True)


def test_resolve_twice_second_returns_false():
    reg = _ConfirmRegistry()
    req_id, _ = reg.open("t", "bash")
    assert reg.resolve(req_id, True)
    assert not reg.resolve(req_id, True)


def test_discard_removes_slot():
    reg = _ConfirmRegistry()
    req_id, _ = reg.open("t", "bash")
    reg.discard(req_id)
    assert not reg.resolve(req_id, True)


def test_cancel_trace_wakes_all_slots_for_trace():
    """cancel_trace leaves slot.result = None (the 'cancelled' signal that
    the hook turns into AbortRound), distinct from explicit user-deny
    which sets result=False."""
    reg = _ConfirmRegistry()
    a1_id, a1 = reg.open("A", "bash")
    a2_id, a2 = reg.open("A", "write_file")
    b_id, b = reg.open("B", "bash")

    reg.cancel_trace("A")

    # Cancelled slots are signalled but their result stays None.
    assert a1.event.is_set() and a1.result is None
    assert a2.event.is_set() and a2.result is None
    # Trace B untouched.
    assert not b.event.is_set()
    assert b.result is None


def test_cancel_trace_unknown_is_noop():
    reg = _ConfirmRegistry()
    reg.cancel_trace("never")  # should not raise


def test_concurrent_open_and_resolve_no_lost_slots():
    """Hammer the registry from multiple threads and check counts add up."""
    reg = _ConfirmRegistry()
    N = 100
    opened: list[str] = []
    open_lock = threading.Lock()

    def opener(i):
        req_id, _ = reg.open(f"trace-{i % 10}", "bash")
        with open_lock:
            opened.append(req_id)

    threads = [threading.Thread(target=opener, args=(i,)) for i in range(N)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(opened) == N
    assert len(set(opened)) == N  # all unique

    # Resolve all of them concurrently.
    resolved = []
    res_lock = threading.Lock()

    def resolver(req_id):
        ok = reg.resolve(req_id, True)
        with res_lock:
            resolved.append(ok)

    threads = [threading.Thread(target=resolver, args=(rid,)) for rid in opened]
    for t in threads: t.start()
    for t in threads: t.join()

    assert all(resolved)


# ── _RegistryConfirmHook ────────────────────────────────────────────────────

class _FakeEngine:
    """Minimal stand-in — the hook only reads `_confirm_registry`. Kept here
    so the test asserts the contract: the hook does NOT touch any other
    engine attributes (no _on_event reads — that was the bug we fixed)."""

    def __init__(self):
        self._confirm_registry = _ConfirmRegistry()


def _events_collected():
    """Make a list-collecting on_event callable."""
    captured = []
    return captured, lambda raw: captured.append(raw)


def test_hook_skips_for_unmatched_tool():
    reg = _ConfirmRegistry()
    captured, on_event = _events_collected()
    hook = _RegistryConfirmHook(reg, on_event, "trace", confirm_tools={"bash"})
    assert hook.run("read_file", {}) == HookResult.SKIP
    assert captured == []


def test_hook_emits_confirm_request_event_with_request_id():
    reg = _ConfirmRegistry()
    captured, on_event = _events_collected()
    hook = _RegistryConfirmHook(reg, on_event, "trace-X", {"bash"})

    # Resolve from another thread so .wait() returns quickly.
    def resolve_soon():
        time.sleep(0.05)
        # Find the slot created by the hook (only one open).
        req_ids = list(reg._slots.keys())
        assert len(req_ids) == 1
        reg.resolve(req_ids[0], True)

    t = threading.Thread(target=resolve_soon)
    t.start()
    result = hook.run("bash", {"command": "ls"})
    t.join()

    assert result == HookResult.ALLOW
    assert len(captured) == 1
    evt = captured[0]
    assert evt["type"] == "confirm_request"
    assert evt["tool_name"] == "bash"
    assert evt["tool_args"] == {"command": "ls"}
    assert "request_id" in evt and evt["request_id"]


def test_hook_returns_deny_when_user_rejects():
    reg = _ConfirmRegistry()
    _, on_event = _events_collected()
    hook = _RegistryConfirmHook(reg, on_event, "trace", {"bash"})

    def reject_soon():
        time.sleep(0.05)
        req_id = next(iter(reg._slots))
        reg.resolve(req_id, False)

    threading.Thread(target=reject_soon).start()
    result = hook.run("bash", {})
    assert result == HookResult.DENY
    assert hook.reason == "User rejected"


def test_hook_aborts_on_timeout(monkeypatch):
    monkeypatch.setattr(config, "HITL_TIMEOUT", 0.1)  # 100ms
    reg = _ConfirmRegistry()
    _, on_event = _events_collected()
    hook = _RegistryConfirmHook(reg, on_event, "trace", {"bash"})
    with pytest.raises(AbortRound) as exc:
        hook.run("bash", {})
    assert "timeout" in exc.value.reason.lower()
    # Slot should be cleaned up.
    assert reg._slots == {}


def test_hook_aborts_when_trace_cancelled():
    reg = _ConfirmRegistry()
    _, on_event = _events_collected()
    hook = _RegistryConfirmHook(reg, on_event, "trace-Y", {"bash"})

    def cancel_soon():
        time.sleep(0.05)
        reg.cancel_trace("trace-Y")

    threading.Thread(target=cancel_soon).start()
    with pytest.raises(AbortRound) as exc:
        hook.run("bash", {})
    assert "disconnected" in exc.value.reason.lower()


def test_two_concurrent_traces_dont_cross_route():
    """Regression for the _on_event bug: two hooks bound to different
    traces with different on_event callbacks must never call each other's."""
    reg = _ConfirmRegistry()
    captured_A, on_event_A = _events_collected()
    captured_B, on_event_B = _events_collected()
    hook_A = _RegistryConfirmHook(reg, on_event_A, "trace-A", {"bash"})
    hook_B = _RegistryConfirmHook(reg, on_event_B, "trace-B", {"bash"})

    barrier = threading.Barrier(2)
    results: dict[str, HookResult] = {}

    def fire(label, hook):
        barrier.wait()  # both fire simultaneously
        results[label] = hook.run("bash", {"who": label})

    def resolve_both():
        time.sleep(0.1)
        # Resolve every pending slot — both should be present.
        for req_id, slot in list(reg._slots.items()):
            reg.resolve(req_id, True)

    threading.Thread(target=fire, args=("A", hook_A)).start()
    threading.Thread(target=fire, args=("B", hook_B)).start()
    threading.Thread(target=resolve_both).start()

    # Wait for both fires to complete (use barrier-bypass + poll).
    deadline = time.time() + 3
    while len(results) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert len(results) == 2

    # Each callback only saw its own trace's confirm_request.
    assert len(captured_A) == 1
    assert len(captured_B) == 1
    assert captured_A[0]["tool_args"] == {"who": "A"}
    assert captured_B[0]["tool_args"] == {"who": "B"}
