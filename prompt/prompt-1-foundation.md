# Prompt 1: Foundation — uv workspace + loop.py callback + engine

## Task
Set up uv workspace, add event callback to agent_runtime, create engine layer

## Context
We are building `agent_frontend` as a separate package that wraps the existing `agent_runtime`.
The strategy: minimal changes to agent_runtime (add ~30 lines of callback hooks), then build
an async engine in agent_frontend that bridges sync→async via queue.

Current state:
- `agent_runtime/` is a working package with `pyproject.toml` at repo root
- `uv.lock` exists (uv is already the package manager)
- `agent_runtime/loop.py` has `_stream_response()` and `agent_loop()` that print to stdout
- `agent_runtime/__main__.py` is the current CLI entry point — DO NOT DELETE IT

## Phase 1: Convert to uv workspace

### 1.1 Update root `pyproject.toml`

Current content:
```toml
[project]
name = "agent-runtime"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "anthropic>=0.25.0",
    "mcp>=1.26.0",
    "python-dotenv>=1.0.0",
]

[project.scripts]
agent = "agent_runtime.__main__:main"
```

Add workspace config at the end:
```toml
[tool.uv.workspace]
members = ["agent_frontend"]

[tool.uv.sources]
agent-runtime = { workspace = true }
```

Keep everything else unchanged. The existing `agent` script entry point must still work.

### 1.2 Create `agent_frontend/pyproject.toml`

```toml
[project]
name = "agent-frontend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "agent-runtime",
]

[project.optional-dependencies]
cli = ["rich>=13.0", "prompt-toolkit>=3.0"]
web = ["fastapi>=0.115", "uvicorn>=0.34", "sse-starlette>=2.0"]
all = ["agent-frontend[cli,web]"]

[project.scripts]
agent-cli = "agent_frontend.cli.app:main"
agent-web = "agent_frontend.web.run:main"

[tool.uv.sources]
agent-runtime = { workspace = true }
```

### 1.3 Create package skeleton

```
agent_frontend/
├── pyproject.toml
├── __init__.py          # empty
├── engine.py            # Phase 3
├── schemas.py           # Phase 3
├── cli/
│   └── __init__.py      # empty (built in Prompt 2)
└── web/
    └── __init__.py      # empty (built in Prompt 3)
```

### 1.4 Run `uv sync`

```bash
cd /Users/wqeq/Desktop/project/agent-runtime
uv sync --all-extras
```

Verify both packages install. Verify `python -m agent_runtime` still works (the existing CLI).

---

## Phase 2: Add on_event callback to agent_runtime/loop.py

This is the surgical modification. Rules:
- **Add** an `on_event` parameter (default `None`) to `_stream_response` and `agent_loop`
- **Keep** all existing `sys.stdout.write` and `print` calls — on_event is ADDITIVE
- When `on_event` is None, behavior is 100% identical to current code
- Event format: `on_event({"type": "...", ...})`

### 2.1 Modify `_stream_response(system, messages)` → `_stream_response(system, messages, on_event=None)`

Add `on_event` calls alongside existing stdout writes. The exact insertion points
in the current code (reference agent_runtime/loop.py):

**Line 144 (thinking block start):**
```python
# EXISTING:
sys.stdout.write("\033[2m")  # dim for thinking
# ADD AFTER:
if on_event:
    on_event({"type": "thinking_start"})
```

**Lines 153-155 (text_delta):**
```python
# EXISTING:
sys.stdout.write(delta.text)
sys.stdout.flush()
current_text += delta.text
# ADD AFTER current_text += :
if on_event:
    on_event({"type": "text_delta", "text": delta.text})
```

**Lines 157-159 (thinking_delta):**
```python
# EXISTING:
sys.stdout.write(delta.thinking)
sys.stdout.flush()
current_thinking += delta.thinking
# ADD AFTER current_thinking += :
if on_event:
    on_event({"type": "thinking_delta", "text": delta.thinking})
```

**Line 167 (text block stop, after sys.stdout.write("\n")):**
```python
if on_event:
    on_event({"type": "text_stop"})
```

**Line 170 (thinking block stop, after sys.stdout.write("\033[0m\n")):**
```python
if on_event:
    on_event({"type": "thinking_stop"})
```

**After the `with` block exits (after line 186, before return):**
```python
if on_event:
    on_event({"type": "message_done", "stop_reason": stop_reason,
              "usage": {"input": usage.input_tokens, "output": usage.output_tokens,
                        "cache_creation": getattr(usage, 'cache_creation_input_tokens', 0),
                        "cache_read": getattr(usage, 'cache_read_input_tokens', 0)}})
```

### 2.2 Modify `agent_loop` signature → add `on_event=None`

```python
def agent_loop(messages: list, system: str, tracker: TokenTracker = None, session=None, on_event=None):
```

**Pass on_event to _stream_response (around line 218):**
```python
content_blocks, stop_reason, usage = _stream_response(system, messages, on_event=on_event)
```

**After token tracking (around line 223, after the print):**
```python
if on_event and tracker and usage:
    on_event({"type": "token_usage",
              "turn": {"input": turn.input_tokens, "output": turn.output_tokens,
                       "cache_creation": turn.cache_creation_input_tokens,
                       "cache_read": turn.cache_read_input_tokens},
              "total": {"input": tracker.total.input_tokens, "output": tracker.total.output_tokens},
              "cost": tracker.format_turn(turn, config.MODEL)})
```

**Before tool dispatch (around line 256, before dispatch_tool call):**
```python
if on_event:
    on_event({"type": "tool_call", "id": block.id, "name": block.name,
              "args": block.input, "args_summary": _format_args(block.name, block.input)})
```

**After tool dispatch (after output is set, before args_summary line ~255):**
```python
if on_event:
    on_event({"type": "tool_result", "id": block.id, "name": block.name,
              "output": str(output)[:3000], "is_error": str(output).startswith("Error:")})
```

**On auto_compact (around line 213-214):**
```python
if on_event:
    on_event({"type": "status", "message": "auto_compact triggered"})
```

**When loop ends (stop_reason != "tool_use", around line 230):**
```python
if on_event:
    on_event({"type": "done", "stop_reason": stop_reason})
```

### 2.3 Verify backward compatibility

The existing `__main__.py` calls `agent_loop(history, system, tracker, session=session)`
with no `on_event` argument → defaults to None → all `if on_event:` blocks are skipped
→ behavior identical. VERIFY THIS by running `python -m agent_runtime` and having a short
conversation.

---

## Phase 3: Create agent_frontend/engine.py and schemas.py

### 3.1 `agent_frontend/schemas.py`

Define event dataclasses with `to_sse()` for Server-Sent Events serialization:

```python
from dataclasses import dataclass, asdict
import json

@dataclass
class EngineEvent:
    type: str
    def to_sse(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"
    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

@dataclass
class TextDelta(EngineEvent):
    type: str = "text_delta"
    text: str = ""

@dataclass
class ThinkingDelta(EngineEvent):
    type: str = "thinking_delta"
    text: str = ""

@dataclass
class ThinkingStart(EngineEvent):
    type: str = "thinking_start"

@dataclass
class ThinkingStop(EngineEvent):
    type: str = "thinking_stop"

@dataclass
class TextStop(EngineEvent):
    type: str = "text_stop"

@dataclass
class ToolCall(EngineEvent):
    type: str = "tool_call"
    id: str = ""
    name: str = ""
    args: dict = None
    args_summary: str = ""

@dataclass
class ToolResult(EngineEvent):
    type: str = "tool_result"
    id: str = ""
    name: str = ""
    output: str = ""
    is_error: bool = False

@dataclass
class TokenUsage(EngineEvent):
    type: str = "token_usage"
    turn: dict = None
    total: dict = None
    cost: str = ""

@dataclass
class Status(EngineEvent):
    type: str = "status"
    message: str = ""

@dataclass
class Done(EngineEvent):
    type: str = "done"
    stop_reason: str = ""

@dataclass
class Error(EngineEvent):
    type: str = "error"
    message: str = ""
```

### 3.2 `agent_frontend/engine.py`

This class mirrors `agent_runtime/__main__.py` lines 40-104 for initialization,
then provides an async streaming interface via queue bridge.

```python
"""Async engine wrapping agent_runtime's synchronous agent_loop."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

from agent_runtime import config
from agent_runtime.sandbox import setup_workspace
from agent_runtime.todo import Todo
from agent_runtime.skills import SkillLoader
from agent_runtime.compression import auto_compact
from agent_runtime.loop import agent_loop, build_system_prompt, _inject_todo
from agent_runtime.mcp_client import MCPManager
from agent_runtime.tracking import TokenTracker
from agent_runtime.hooks import HookManager, HumanConfirmHook
from agent_runtime.session import SessionStore
from agent_runtime import tools as tools_mod

from .schemas import (
    EngineEvent, TextDelta, ThinkingDelta, ThinkingStart, ThinkingStop, TextStop,
    ToolCall, ToolResult, TokenUsage, Status, Done, Error,
)

@dataclass
class EngineConfig:
    workspace: Optional[str] = None
    thinking: bool = False
    thinking_budget: int = 10000
    mcp_config: Optional[str] = None
    confirm: bool = False
    keep_sandbox: bool = False


# Map raw event dict from loop.py on_event callback → EngineEvent dataclass
_EVENT_MAP = {
    "text_delta": lambda d: TextDelta(text=d["text"]),
    "thinking_delta": lambda d: ThinkingDelta(text=d["text"]),
    "thinking_start": lambda d: ThinkingStart(),
    "thinking_stop": lambda d: ThinkingStop(),
    "text_stop": lambda d: TextStop(),
    "tool_call": lambda d: ToolCall(id=d["id"], name=d["name"], args=d.get("args", {}),
                                     args_summary=d.get("args_summary", "")),
    "tool_result": lambda d: ToolResult(id=d["id"], name=d["name"],
                                         output=d.get("output", ""),
                                         is_error=d.get("is_error", False)),
    "token_usage": lambda d: TokenUsage(turn=d.get("turn"), total=d.get("total"),
                                         cost=d.get("cost", "")),
    "status": lambda d: Status(message=d.get("message", "")),
    "done": lambda d: Done(stop_reason=d.get("stop_reason", "")),
    "message_done": lambda d: Done(stop_reason=d.get("stop_reason", "")),
}


class AgentEngine:
    """Wraps agent_runtime for use by CLI and Web frontends."""

    def __init__(self, cfg: EngineConfig):
        # --- Initialization mirrors __main__.py lines 74-113 ---
        if cfg.keep_sandbox:
            config.SANDBOX_MODE = "persistent"
        setup_workspace(cfg.workspace)

        config.THINKING_ENABLED = cfg.thinking
        config.THINKING_BUDGET = cfg.thinking_budget

        self.todo = Todo()
        self.skill_loader = SkillLoader(config.WORKDIR / "skills")
        self.tracker = TokenTracker()
        self.mcp = MCPManager()
        self.hooks = HookManager()
        self.session_store = SessionStore()

        if cfg.confirm:
            self.hooks.add(HumanConfirmHook())

        # Wire into tools module
        tools_mod.TODO = self.todo
        tools_mod.SKILL_LOADER = self.skill_loader
        tools_mod.MCP = self.mcp
        tools_mod.HOOKS = self.hooks

        # MCP
        mcp_path = Path(cfg.mcp_config) if cfg.mcp_config else config.WORKDIR / "mcp.json"
        mcp_cfg = self.mcp.load_config(mcp_path)
        if mcp_cfg.get("servers"):
            self.mcp.start(mcp_cfg)
            tools_mod.rebuild_tools()

        self.system = build_system_prompt(self.skill_loader, mcp_manager=self.mcp)

        # Session state: session_id → history list
        self._sessions: dict[str, list] = {}
        self._executor = ThreadPoolExecutor(max_workers=2)

    # --- Startup info (for frontend banners) ---
    @property
    def startup_info(self) -> dict:
        return {
            "workspace": str(config.WORKDIR),
            "model": config.MODEL,
            "sandbox_enabled": config.SANDBOX_ENABLED,
            "sandbox_mode": config.SANDBOX_MODE if config.SANDBOX_ENABLED else None,
            "thinking": config.THINKING_ENABLED,
            "thinking_budget": config.THINKING_BUDGET if config.THINKING_ENABLED else None,
            "mcp_tool_count": len(self.mcp.tool_names),
            "mcp_server_count": len(self.mcp._servers) if self.mcp.tool_names else 0,
        }

    # --- Session management ---
    def create_session(self) -> str:
        self.session_store.new_session()
        sid = self.session_store.session_id
        self._sessions[sid] = []
        return sid

    def load_session(self, session_id: str) -> list[dict]:
        history = self.session_store.load_session(session_id)
        self._sessions[session_id] = history
        return history

    def list_sessions(self) -> list[dict]:
        return self.session_store.list_sessions()

    def _get_history(self, session_id: str) -> list:
        if session_id not in self._sessions:
            self.load_session(session_id)
        return self._sessions[session_id]

    # --- Core: async streaming chat ---
    async def chat_stream(self, session_id: str, user_message: str) -> AsyncGenerator[EngineEvent, None]:
        history = self._get_history(session_id)
        history.append({"role": "user", "content": user_message})
        self.session_store.save_turn(history[-1])

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[Optional[EngineEvent]] = asyncio.Queue()

        def on_event(raw: dict):
            evt_type = raw.get("type", "")
            factory = _EVENT_MAP.get(evt_type)
            if factory:
                evt = factory(raw)
                loop.call_soon_threadsafe(queue.put_nowait, evt)

        def _run_sync():
            try:
                agent_loop(history, self.system, self.tracker,
                           session=self.session_store, on_event=on_event)
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, Error(message=str(e)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

        future = loop.run_in_executor(self._executor, _run_sync)

        while True:
            event = await queue.get()
            if event is None:
                break
            yield event

        await future  # ensure thread completed

    # --- Commands ---
    def compact(self, session_id: str) -> str:
        history = self._get_history(session_id)
        history[:] = auto_compact(history, self.tracker)
        _inject_todo(history)
        return "compacted"

    def get_todo(self) -> str:
        return self.todo.read()

    def get_tools(self) -> list[str]:
        from agent_runtime.tools import TOOLS
        return [t["name"] for t in TOOLS]

    def get_skills(self) -> list[dict]:
        return self.skill_loader.get_descriptions() if self.skill_loader else []

    # --- Cleanup ---
    def shutdown(self):
        self.mcp.shutdown()
        self._executor.shutdown(wait=False)
```

**Important implementation notes:**
- The `on_event` callback is called from the sync thread running `agent_loop`
- `loop.call_soon_threadsafe(queue.put_nowait, evt)` bridges sync→async safely
- The `None` sentinel signals the async generator to stop
- Errors in the sync thread are caught and forwarded as `Error` events
- The engine maintains per-session history dicts so multiple sessions can coexist
  (note: agent_runtime's global state in tools.py means true concurrency isn't safe,
   but sequential session switching works fine)

---

## Verification Steps

Run these yourself each iteration:

- [ ] `uv sync --all-extras` completes without error
- [ ] `python -c "from agent_runtime.loop import agent_loop; import inspect; assert 'on_event' in inspect.signature(agent_loop).parameters"` — callback parameter exists
- [ ] `python -m agent_runtime` still launches the original REPL (backward compat)
- [ ] Send a test message in the original REPL — verify output looks identical to before
- [ ] `python -c "from agent_frontend.engine import AgentEngine, EngineConfig"` imports OK
- [ ] `python -c "from agent_frontend.schemas import TextDelta, ThinkingDelta, ToolCall, ToolResult, TokenUsage, Done, Error; print(TextDelta(text='hi').to_sse())"` prints valid SSE
- [ ] Write a quick smoke test script `_test_engine.py`:
  ```python
  import asyncio
  from agent_frontend.engine import AgentEngine, EngineConfig
  async def main():
      engine = AgentEngine(EngineConfig(workspace="."))
      sid = engine.create_session()
      print(f"Session: {sid}")
      print(f"Startup: {engine.startup_info}")
      print(f"Tools: {engine.get_tools()[:5]}")
      async for event in engine.chat_stream(sid, "Say hello in one sentence."):
          print(f"  [{event.type}] {str(event)[:100]}")
      engine.shutdown()
  asyncio.run(main())
  ```
  Run it: `uv run python _test_engine.py` — verify events stream correctly, no crashes
- [ ] Verify agent_runtime/loop.py diff is under 40 lines added (use `git diff --stat`)
- [ ] Verify NO other files in agent_runtime/ were modified (only loop.py)

## What NOT to do
- Do NOT delete agent_runtime/__main__.py
- Do NOT modify any agent_runtime file except loop.py
- Do NOT add new dependencies to agent_runtime's pyproject.toml
- Do NOT use pip — use uv only
- Do NOT add OAuth, Postgres, or Redis

After all verification steps pass and the smoke test prints streaming events, output: <promise>COMPLETE</promise>
