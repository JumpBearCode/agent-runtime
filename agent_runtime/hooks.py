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
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


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

    def __init__(self, confirm_tools: set[str]):
        self.confirm_tools = confirm_tools
        self.reason = ""

    def run(self, name: str, args: dict) -> HookResult:
        if name not in self.confirm_tools:
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


def load_confirm_tools(path: Path) -> set[str]:
    """Load tool names that require HITL confirmation from JSON."""
    from .tools import TOOLS
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return set()
    except json.JSONDecodeError as e:
        logger.warning("HITL.json: invalid JSON — %s", e)
        return set()
    if not isinstance(data, list):
        logger.warning("HITL.json: expected a JSON array, got %s", type(data).__name__)
        return set()
    valid_names = {t["name"] for t in TOOLS}
    result = set()
    for name in data:
        if name not in valid_names:
            logger.warning("HITL.json: '%s' is not a known tool, skipping", name)
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
