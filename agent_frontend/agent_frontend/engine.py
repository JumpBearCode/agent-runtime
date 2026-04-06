"""Async engine wrapping agent_runtime's synchronous agent_loop."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

from agent_runtime import config
from agent_runtime.todo import Todo
from agent_runtime.skills import SkillLoader
from agent_runtime.compression import auto_compact
from agent_runtime.loop import agent_loop, build_system_prompt, _inject_todo
from agent_runtime.mcp_client import MCPManager
from agent_runtime.tracking import TokenTracker
from agent_runtime.hooks import HookManager, HookResult, PreToolHook, _preview, validate_hitl
from agent_runtime.session import SessionStore
from agent_runtime import tools as tools_mod

from .schemas import (
    EngineEvent, TextDelta, ThinkingDelta, ThinkingStart, ThinkingStop, TextStop,
    ToolCall, ToolResult, TokenUsage, Status, Done, Error, ConfirmRequest,
)


@dataclass
class EngineConfig:
    workspace: Optional[str] = None
    thinking: bool = False
    thinking_budget: int = 10000
    settings: Optional[str] = None

    def apply(self):
        """Push values to the global config module."""
        if self.workspace:
            ws = Path(self.workspace).resolve() if self.workspace != "." else Path.cwd()
            if not ws.exists():
                ws.mkdir(parents=True)
            config.WORKDIR = ws
        config.THINKING_ENABLED = self.thinking
        config.THINKING_BUDGET = self.thinking_budget
        config.SETTINGS_OVERRIDE = self.settings


# Map raw event dict from loop.py on_event callback -> EngineEvent dataclass
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
    "confirm_request": lambda d: ConfirmRequest(
        tool_name=d.get("tool_name", ""), tool_args=d.get("tool_args", {}),
        preview=d.get("preview", "")),
}


class _EngineConfirmHook(PreToolHook):
    """Confirm hook that bridges to the frontend via on_event + threading.Event."""

    def __init__(self, engine: "AgentEngine", confirm_tools: set[str]):
        self._engine = engine
        self.confirm_tools = confirm_tools
        self.reason = ""

    def run(self, name: str, args: dict) -> HookResult:
        if name not in self.confirm_tools:
            return HookResult.SKIP
        preview = _preview(name, args)
        # Send confirm_request through the on_event callback (set by chat_stream)
        if self._engine._on_event:
            self._engine._confirm_pending.clear()
            self._engine._confirm_result = False
            self._engine._on_event({
                "type": "confirm_request",
                "tool_name": name, "tool_args": args, "preview": preview,
            })
            # Block until frontend calls respond_confirm()
            self._engine._confirm_pending.wait(timeout=300)
            if self._engine._confirm_result:
                return HookResult.ALLOW
            self.reason = "User rejected"
            return HookResult.DENY
        # No event callback (shouldn't happen) — allow by default
        return HookResult.ALLOW


class AgentEngine:
    """Wraps agent_runtime for use by CLI and Web frontends."""

    def __init__(self, cfg: EngineConfig):
        cfg.apply()

        self.todo = Todo()
        self.skill_loader = SkillLoader(config.WORKDIR / "skills")
        self.tracker = TokenTracker()
        self.mcp = MCPManager()
        self.hooks = HookManager()
        self.session_store = SessionStore()

        # Confirm bridge state
        self._confirm_pending = threading.Event()
        self._confirm_result = False
        self._on_event = None  # set per chat_stream call

        # Wire into tools module
        tools_mod.TODO = self.todo
        tools_mod.SKILL_LOADER = self.skill_loader
        tools_mod.MCP = self.mcp
        tools_mod.HOOKS = self.hooks

        # MCP: resolve from settings layers and connect
        mcp_cfg = config.resolve_mcp_config()
        if mcp_cfg.get("servers"):
            self.mcp.start(mcp_cfg)
            tools_mod.rebuild_tools()

        # Confirm hook — after MCP so TOOLS is fully populated for validation.
        # Registered iff HITL.json resolves to a non-empty tool set.
        confirm_set = validate_hitl(config.resolve_hitl())
        if confirm_set:
            self.hooks.add(_EngineConfirmHook(self, confirm_tools=confirm_set))
            config.CONFIRM = True

        self.system = build_system_prompt(self.skill_loader, mcp_manager=self.mcp)

        # Session state: session_id -> history list
        self._sessions: dict[str, list] = {}
        self._executor = ThreadPoolExecutor(max_workers=2)

    @property
    def startup_info(self) -> dict:
        return {
            "workspace": str(config.WORKDIR),
            "model": config.MODEL,
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

        self._on_event = on_event  # expose for EngineConfirmHook

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

    def get_skills(self) -> str:
        return self.skill_loader.get_descriptions() if self.skill_loader else ""

    def get_skill_names(self) -> dict[str, str]:
        """Return {name: description} for all loaded skills."""
        if not self.skill_loader:
            return {}
        return {
            name: skill["meta"].get("description", "")
            for name, skill in self.skill_loader.skills.items()
        }

    def get_skill_content(self, name: str) -> str | None:
        """Return skill content if name is valid, else None."""
        if self.skill_loader and name in self.skill_loader.skills:
            return self.skill_loader.get_content(name)
        return None

    def respond_confirm(self, allowed: bool):
        """Called by frontend when user responds to a confirm_request."""
        self._confirm_result = allowed
        self._confirm_pending.set()

    def shutdown(self):
        self._on_event = None
        self.mcp.shutdown()
        self._executor.shutdown(wait=False)
