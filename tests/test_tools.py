"""tools.py: file IO, bash, dispatch routing, thread-local hook/todo isolation."""

import threading

import pytest

from agent_runtime.core import tools as tools_mod
from agent_runtime.core.hooks import AbortRound, HookManager, HookResult, PreToolHook
from agent_runtime.core.todo import Todo


# ── safe_path ──────────────────────────────────────────────────────────────

def test_safe_path_inside_workspace(workspace):
    p = tools_mod.safe_path("subdir/file.txt")
    assert str(p).startswith(str(workspace))


def test_safe_path_blocks_traversal(workspace):
    with pytest.raises(ValueError, match="escapes workspace"):
        tools_mod.safe_path("../../../etc/passwd")


# ── file IO ────────────────────────────────────────────────────────────────

def test_run_write_then_read(workspace):
    out = tools_mod.run_write("hello.txt", "world")
    assert "Wrote" in out
    assert tools_mod.run_read("hello.txt") == "world"


def test_run_write_creates_parent_dirs(workspace):
    tools_mod.run_write("a/b/c/file.txt", "deep")
    assert tools_mod.run_read("a/b/c/file.txt") == "deep"


def test_run_edit_replaces_text(workspace):
    tools_mod.run_write("f.txt", "alpha bravo")
    out = tools_mod.run_edit("f.txt", "bravo", "charlie")
    assert "Edited" in out
    assert tools_mod.run_read("f.txt") == "alpha charlie"


def test_run_edit_missing_text_returns_error(workspace):
    tools_mod.run_write("f.txt", "alpha")
    out = tools_mod.run_edit("f.txt", "missing", "x")
    assert out.startswith("Error: Text not found")


def test_run_read_truncates_with_limit(workspace):
    tools_mod.run_write("big.txt", "\n".join(str(i) for i in range(100)))
    out = tools_mod.run_read("big.txt", limit=5)
    lines = out.splitlines()
    assert lines[:5] == ["0", "1", "2", "3", "4"]
    assert "more" in lines[-1]


# ── bash ───────────────────────────────────────────────────────────────────

def test_run_bash_echo(workspace):
    out = tools_mod.run_bash("echo hello")
    assert out.strip() == "hello"


def test_run_bash_blocks_dangerous():
    out = tools_mod.run_bash("sudo rm -rf /")
    assert out.startswith("Error: Dangerous")


# ── dispatch_tool ──────────────────────────────────────────────────────────

def test_dispatch_routes_to_handler(workspace):
    out = tools_mod.dispatch_tool("write_file", {"path": "x.txt", "content": "hi"})
    assert "Wrote" in out


def test_dispatch_unknown_tool():
    out = tools_mod.dispatch_tool("not_a_tool", {})
    assert out.startswith("Unknown tool")


def test_dispatch_blocked_by_hook(workspace):
    class _Block(PreToolHook):
        def __init__(self):
            self.reason = "no bash for you"

        def run(self, name, args):
            return HookResult.DENY if name == "bash" else HookResult.SKIP

    mgr = HookManager()
    mgr.add(_Block())
    tools_mod.set_thread_hooks(mgr)
    out = tools_mod.dispatch_tool("bash", {"command": "echo x"})
    assert out == "Blocked: no bash for you"


def test_dispatch_propagates_abort_round(workspace):
    class _Abort(PreToolHook):
        def run(self, name, args):
            raise AbortRound("test abort")

    mgr = HookManager()
    mgr.add(_Abort())
    tools_mod.set_thread_hooks(mgr)
    with pytest.raises(AbortRound) as exc:
        tools_mod.dispatch_tool("bash", {"command": "echo"})
    assert exc.value.reason == "test abort"


# ── thread-local isolation (regression for cross-chat pollution bugs) ─────

def test_thread_local_todo_isolation():
    """Two threads each set their own Todo; neither sees the other's."""
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def worker(label):
        todo = Todo()
        tools_mod.set_thread_todo(todo)
        todo.write([{"id": 1, "content": f"task-{label}", "status": "pending"}])
        barrier.wait()  # ensure both have written before any reads
        results[label] = tools_mod.active_todo().read()
        tools_mod.set_thread_todo(None)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert "task-A" in results["A"]
    assert "task-A" not in results["B"]
    assert "task-B" in results["B"]
    assert "task-B" not in results["A"]


def test_thread_local_hooks_isolation(workspace):
    """Two threads each install a different hook; dispatch_tool sees the right one."""
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    class _Reason(PreToolHook):
        def __init__(self, reason):
            self.reason = reason

        def run(self, name, args):
            return HookResult.DENY

    def worker(label):
        mgr = HookManager()
        mgr.add(_Reason(f"deny-{label}"))
        tools_mod.set_thread_hooks(mgr)
        barrier.wait()
        results[label] = tools_mod.dispatch_tool("bash", {"command": "x"})
        tools_mod.set_thread_hooks(None)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results["A"] == "Blocked: deny-A"
    assert results["B"] == "Blocked: deny-B"


def test_active_hooks_falls_back_to_module_global():
    """If no thread-local set, _active_hooks reads module-level HOOKS."""
    sentinel = object()
    tools_mod.HOOKS = sentinel
    try:
        assert tools_mod._active_hooks() is sentinel
    finally:
        tools_mod.HOOKS = None


def test_active_todo_falls_back_to_module_global():
    sentinel = object()
    tools_mod.TODO = sentinel
    try:
        assert tools_mod.active_todo() is sentinel
    finally:
        tools_mod.TODO = None
