"""Hook system — intercept tool calls before execution.

Usage:
    # Subclass PreToolHook for custom behavior
    class MyHook(PreToolHook):
        def run(self, name, args):
            print(f"About to call {name}")
            return HookResult.ALLOW

    # Register globally or per-tool
    hooks = HookManager()
    hooks.add(MyHook())                           # all tools
    hooks.add(MyHook(), tools=["bash"])            # only bash
    hooks.add(MyHook(), tools=["mcp_github_*"])    # all tools from MCP github server

    # In the loop, before executing a tool:
    result = hooks.before_tool("bash", {"command": "ls"})
    if not result.allowed:
        output = f"Blocked: {result.reason}"
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class HookResult(Enum):
    ALLOW = "allow"
    DENY = "deny"
    SKIP = "skip"   # this hook has no opinion, continue to next


@dataclass
class HookDecision:
    allowed: bool
    reason: str = ""


class AbortRound(Exception):
    """Raised by a hook to terminate the current agent round immediately.

    The agent loop catches this, backfills tool_results for the in-flight
    tool_use and any remaining unprocessed tool_use blocks (Anthropic API
    requires every tool_use to have a matching tool_result), saves the
    session, emits a `done` event with stop_reason=hitl_timeout, and returns.
    The thread is released; conversation history stays well-formed so the
    user can resume in a new turn.
    """

    def __init__(self, reason: str = ""):
        self.reason = reason
        super().__init__(reason)


class PreToolHook:
    """Base class for pre-tool-use hooks. Subclass and override run()."""

    def run(self, name: str, args: dict) -> HookResult:
        """Decide whether to allow this tool call.

        Returns:
            HookResult.ALLOW — allow and stop checking further hooks
            HookResult.DENY  — block and stop checking further hooks
            HookResult.SKIP  — no opinion, continue to next hook
        """
        return HookResult.SKIP


@dataclass
class _RegisteredHook:
    hook: PreToolHook
    patterns: list[str] = field(default_factory=list)  # empty = all tools

    def matches(self, tool_name: str) -> bool:
        if not self.patterns:
            return True
        return any(fnmatch.fnmatch(tool_name, p) for p in self.patterns)


class HookManager:
    def __init__(self):
        self._pre_hooks: list[_RegisteredHook] = []

    def add(self, hook: PreToolHook, tools: list[str] | None = None):
        """Register a pre-tool hook.

        Args:
            hook: PreToolHook instance
            tools: optional list of tool name patterns (supports glob).
                   Examples: ["bash"], ["mcp_github_*"], ["write_file", "edit_file"]
                   If None, hook applies to all tools.
        """
        self._pre_hooks.append(_RegisteredHook(hook=hook, patterns=tools or []))

    def before_tool(self, name: str, args: dict) -> HookDecision:
        """Run all matching pre-hooks in order. First ALLOW/DENY wins."""
        for entry in self._pre_hooks:
            if not entry.matches(name):
                continue
            result = entry.hook.run(name, args)
            if result == HookResult.ALLOW:
                return HookDecision(allowed=True)
            if result == HookResult.DENY:
                reason = getattr(entry.hook, 'reason', '') or f"Blocked by {entry.hook.__class__.__name__}"
                return HookDecision(allowed=False, reason=reason)
            # SKIP → continue to next hook
        # No hook had an opinion → default allow
        return HookDecision(allowed=True)


# ---------------------------------------------------------------------------
# Built-in hooks
# ---------------------------------------------------------------------------

class LogHook(PreToolHook):
    """Log every tool call (always SKIPs, never blocks)."""

    def __init__(self):
        self.log: list[dict] = []

    def run(self, name: str, args: dict) -> HookResult:
        self.log.append({"tool": name, "args": args})
        return HookResult.SKIP


def validate_hitl(names: set[str]) -> set[str]:
    """Validate tool names against known TOOLS, warn and skip unknowns."""
    from .tools import TOOLS
    valid_names = {t["name"] for t in TOOLS}
    result = set()
    for name in names:
        if name not in valid_names:
            logger.warning("HITL: '%s' is not a known tool, skipping", name)
        else:
            result.add(name)
    return result


def _preview(name: str, args: dict) -> str:
    """Short preview of tool args for confirmation prompt."""
    if name == "bash":
        return f'  $ {args.get("command", "")[:80]}'
    if name in ("write_file", "edit_file"):
        return f'  {args.get("path", "")}'
    if 'snowflake' in name:
        sql = args.get("sql", "") or args.get("table_name", "") or ""
        return f"\n   {sql}" if sql else ""
    return ""
