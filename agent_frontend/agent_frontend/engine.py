"""Async engine wrapping agent_runtime's synchronous agent_loop."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import dataclass
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
}


class AgentEngine:
    """Wraps agent_runtime for use by CLI and Web frontends."""

    def __init__(self, cfg: EngineConfig):
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

        # Session state: session_id -> history list
        self._sessions: dict[str, list] = {}
        self._executor = ThreadPoolExecutor(max_workers=2)

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

    def shutdown(self):
        self.mcp.shutdown()
        self._executor.shutdown(wait=False)
