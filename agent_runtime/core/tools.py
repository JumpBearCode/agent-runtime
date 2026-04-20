"""Tool implementations, schemas, and dispatch table."""

import subprocess
import threading
from pathlib import Path

from . import config

# Module-level wiring set by the engine at startup.
SKILL_LOADER = None  # SkillLoader instance — read-only, safe to share
MCP = None           # MCPManager instance — read-only from agent's POV
TODO = None          # Fallback Todo (used only if no thread-local set)
HOOKS = None         # Fallback HookManager (used only if no thread-local set)

# Per-thread state. The engine binds these inside every agent thread so
# concurrent chats never cross-contaminate (each chat has its own Todo and
# its own HookManager with a confirm hook bound to its own trace_id).
_thread_state = threading.local()


def set_thread_hooks(hooks) -> None:
    """Bind a HookManager to the current thread (call from inside the agent
    thread, not from the asyncio event loop)."""
    _thread_state.hooks = hooks


def set_thread_todo(todo) -> None:
    """Bind a Todo instance to the current thread."""
    _thread_state.todo = todo


def _active_hooks():
    return getattr(_thread_state, "hooks", None) or HOOKS


def active_todo():
    """Return the current thread's Todo (or the module fallback). Public so
    loop.py can introspect todo state for system-prompt injection."""
    return getattr(_thread_state, "todo", None) or TODO


def safe_path(p: str) -> Path:
    path = (config.WORKDIR / p).resolve()
    if not path.is_relative_to(config.WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=config.WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- Dispatch table (lambdas capture module-level refs, resolved at call time) --
TOOL_HANDLERS = {
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo_write":       lambda **kw: active_todo().write(kw["items"]),
    "todo_read":        lambda **kw: active_todo().read(),
    "load_skill":       lambda **kw: SKILL_LOADER.get_content(kw["name"]),
}

# -- Tool schemas --
CHILD_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]

BUILTIN_TOOLS = CHILD_TOOLS + [
    {"name": "todo_write", "description": "Write or replace the entire todo list. Use to plan multi-step work and track progress.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "content", "status"]}}}, "required": ["items"]}},
    {"name": "todo_read", "description": "Read the current todo list.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "load_skill", "description": "Load specialized knowledge by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "Skill name to load"}}, "required": ["name"]}},
]

# TOOLS is built at startup: built-in + MCP tools
TOOLS = list(BUILTIN_TOOLS)


def rebuild_tools():
    """Rebuild TOOLS list after MCP servers are connected."""
    global TOOLS
    TOOLS = list(BUILTIN_TOOLS)
    if MCP and MCP.tool_schemas:
        TOOLS.extend(MCP.tool_schemas)


def dispatch_tool(name: str, args: dict) -> str:
    """Dispatch a tool call — runs hooks, then routes to handler or MCP."""
    hooks = _active_hooks()
    if hooks:
        decision = hooks.before_tool(name, args)
        if not decision.allowed:
            return f"Blocked: {decision.reason}"
    # Check built-in first
    handler = TOOL_HANDLERS.get(name)
    if handler:
        return handler(**args)
    # Check MCP
    if MCP and name in MCP.tool_names:
        return MCP.call_tool(name, args)
    return f"Unknown tool: {name}"
