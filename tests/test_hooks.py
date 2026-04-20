"""HookManager: pre-tool hook precedence + AbortRound + validate_hitl."""

import logging

import pytest

from agent_runtime.core.hooks import (
    AbortRound,
    HookDecision,
    HookManager,
    HookResult,
    LogHook,
    PreToolHook,
    validate_hitl,
)


class _AlwaysSkip(PreToolHook):
    def run(self, name, args):
        return HookResult.SKIP


class _AlwaysAllow(PreToolHook):
    def run(self, name, args):
        return HookResult.ALLOW


class _AlwaysDeny(PreToolHook):
    def __init__(self):
        self.reason = "denied for tests"

    def run(self, name, args):
        return HookResult.DENY


def test_no_hooks_means_allow():
    mgr = HookManager()
    decision = mgr.before_tool("bash", {"command": "ls"})
    assert isinstance(decision, HookDecision)
    assert decision.allowed


def test_skip_falls_through_to_next_hook():
    mgr = HookManager()
    mgr.add(_AlwaysSkip())
    mgr.add(_AlwaysDeny())
    decision = mgr.before_tool("bash", {})
    assert not decision.allowed
    assert "denied" in decision.reason


def test_allow_short_circuits():
    mgr = HookManager()
    mgr.add(_AlwaysAllow())
    mgr.add(_AlwaysDeny())
    assert mgr.before_tool("bash", {}).allowed


def test_pattern_matches_specific_tool():
    mgr = HookManager()
    mgr.add(_AlwaysDeny(), tools=["bash"])
    assert not mgr.before_tool("bash", {}).allowed
    assert mgr.before_tool("read_file", {}).allowed


def test_pattern_glob_matches():
    mgr = HookManager()
    mgr.add(_AlwaysDeny(), tools=["mcp_github_*"])
    assert not mgr.before_tool("mcp_github_create_issue", {}).allowed
    assert mgr.before_tool("mcp_other_tool", {}).allowed


def test_abort_round_propagates_through_manager():
    class _Aborts(PreToolHook):
        def run(self, name, args):
            raise AbortRound("bye")

    mgr = HookManager()
    mgr.add(_Aborts())
    with pytest.raises(AbortRound) as exc:
        mgr.before_tool("bash", {})
    assert exc.value.reason == "bye"


def test_log_hook_records_calls_and_skips():
    h = LogHook()
    mgr = HookManager()
    mgr.add(h)
    mgr.add(_AlwaysAllow())  # so SKIP from LogHook still resolves
    mgr.before_tool("bash", {"command": "x"})
    mgr.before_tool("read_file", {"path": "/y"})
    assert len(h.log) == 2
    assert h.log[0]["tool"] == "bash"
    assert h.log[1]["args"] == {"path": "/y"}


def test_validate_hitl_filters_unknown_tool_names(caplog):
    from agent_runtime.core import tools as tools_mod
    known = {t["name"] for t in tools_mod.TOOLS}
    pick = next(iter(known))
    with caplog.at_level(logging.WARNING):
        result = validate_hitl({pick, "does-not-exist"})
    assert result == {pick}
    assert any("does-not-exist" in r.message for r in caplog.records)


def test_validate_hitl_empty():
    assert validate_hitl(set()) == set()
