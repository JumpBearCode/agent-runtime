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
from dataclasses import dataclass, field
from enum import Enum


class HookResult(Enum):
    ALLOW = "allow"
    DENY = "deny"
    SKIP = "skip"   # this hook has no opinion, continue to next


@dataclass
class HookDecision:
    allowed: bool
    reason: str = ""


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

class HumanConfirmHook(PreToolHook):
    """Ask the user to confirm before executing dangerous tools."""

    AUTO_ALLOW = {"read_file", "task_create", "task_update", "task_list",
                  "task_get", "load_skill", "check_background", "compact"}

    def __init__(self, auto_allow: set[str] | None = None):
        self.auto_allow = auto_allow if auto_allow is not None else self.AUTO_ALLOW
        self.reason = ""

    def run(self, name: str, args: dict) -> HookResult:
        if name in self.auto_allow:
            return HookResult.SKIP
        # Show what's about to happen
        preview = _preview(name, args)
        try:
            resp = input(f"  \033[35m? Allow {name}{preview}? [Y/n]\033[0m ")
        except (EOFError, KeyboardInterrupt):
            self.reason = "User cancelled"
            return HookResult.DENY
        if resp.strip().lower() in ("", "y", "yes"):
            return HookResult.ALLOW
        self.reason = "User rejected"
        return HookResult.DENY


class LogHook(PreToolHook):
    """Log every tool call (always SKIPs, never blocks)."""

    def __init__(self):
        self.log: list[dict] = []

    def run(self, name: str, args: dict) -> HookResult:
        self.log.append({"tool": name, "args": args})
        return HookResult.SKIP


def _preview(name: str, args: dict) -> str:
    """Short preview of tool args for confirmation prompt."""
    if name == "bash":
        return f'  $ {args.get("command", "")[:80]}'
    if name in ("write_file", "edit_file"):
        return f'  {args.get("path", "")}'
    if name == "subagent":
        return f'  {args.get("description", "")[:60]}'
    if name == "background_run":
        return f'  $ {args.get("command", "")[:80]}'
    return ""
