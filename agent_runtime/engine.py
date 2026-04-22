"""Async engine wrapping the sync agent_runtime.core agent_loop.

Stateless: every chat request carries its full message history. The engine
holds no per-conversation state — it's a pure compute service. Persistence
(chat threads, conversation rounds) is the frontend's responsibility.

Concurrency model:
  • Each /api/chat request runs agent_loop on a thread from a ThreadPoolExecutor.
  • All tools inside that round execute serially on the same thread.
  • The FastAPI event loop never blocks; it only awaits an asyncio.Queue
    that the agent thread feeds via loop.call_soon_threadsafe.

HITL:
  • The agent thread blocks on its OWN threading.Event (per-request, keyed
    by request_id). Other chats are unaffected.
  • POST /api/confirm/{request_id} resolves exactly one slot.
  • If the slot is not resolved within AGENT_HITL_TIMEOUT (default 600s),
    the hook raises AbortRound — agent_loop backfills tool_results and the
    round ends cleanly. The thread is released; the frontend keeps its
    history (it sent it in to begin with) and can resend on the next turn.
  • If the SSE connection drops, all of that trace's pending confirms are
    cancelled the same way.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from auth import (
    UserIdentity,
    build_providers,
    set_current_user,
    reset_current_user,
    set_prompt_callback,
)

from .core import config
from .core.hooks import AbortRound, HookManager, HookResult, PreToolHook, _preview, validate_hitl
from .core.loop import agent_loop, build_system_prompt
from .core.mcp_client import MCPManager
from .core.skills import SkillLoader
from .core import tools as tools_mod
from .core.todo import Todo
from .core.tracking import TokenTracker

from .api.schemas import (
    ConfirmRequest, DeviceFlowRequest, Done, EngineEvent, Error, Status,
    TextDelta, TextStop, ThinkingDelta, ThinkingStart, ThinkingStop,
    TokenUsage, ToolCall, ToolResult,
)

logger = logging.getLogger(__name__)


# ── HITL registry ──────────────────────────────────────────────────────────

@dataclass
class ConfirmSlot:
    """One pending HITL confirm request. The agent thread waits on `event`."""
    event:      threading.Event
    trace_id:   str
    tool_name:  str
    created_at: float
    result:     Optional[bool] = None   # True=allow, False=deny, None=timeout/cancel


class _ConfirmRegistry:
    """Per-engine registry of pending HITL confirms.

    Both the agent thread and the FastAPI event loop touch this dict; we use
    threading.Lock for the dict mutations (microsecond-scale, safe to acquire
    from async code with `with lock:`) and threading.Event on each slot for
    the agent thread to block on.
    """

    def __init__(self):
        self._slots: dict[str, ConfirmSlot] = {}
        # Reverse index for SSE-disconnect cleanup: which req_ids belong to which trace.
        self._by_trace: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def open(self, trace_id: str, tool_name: str) -> tuple[str, ConfirmSlot]:
        req_id = uuid.uuid4().hex
        slot = ConfirmSlot(
            event=threading.Event(),
            trace_id=trace_id,
            tool_name=tool_name,
            created_at=time.time(),
        )
        with self._lock:
            self._slots[req_id] = slot
            self._by_trace.setdefault(trace_id, set()).add(req_id)
        return req_id, slot

    def resolve(self, req_id: str, allowed: bool) -> bool:
        """Set the slot's result and unblock the waiting agent thread.

        Returns True if a slot was found and resolved, False if the request
        had already timed out, been cancelled, or never existed.
        """
        with self._lock:
            slot = self._slots.pop(req_id, None)
            if slot is not None:
                self._by_trace.get(slot.trace_id, set()).discard(req_id)
        if slot is None:
            return False
        slot.result = allowed
        slot.event.set()
        return True

    def discard(self, req_id: str) -> None:
        """Remove a slot without resolving — caller already woke up (timeout)."""
        with self._lock:
            slot = self._slots.pop(req_id, None)
            if slot is not None:
                self._by_trace.get(slot.trace_id, set()).discard(req_id)

    def cancel_trace(self, trace_id: str) -> None:
        """SSE disconnected — cancel every pending confirm for this trace.

        Leaves slot.result = None (the dataclass default) so the hook can
        distinguish this from an explicit user-deny (result=False). On a
        cancelled trace the hook raises AbortRound to end the round
        immediately; on an explicit deny it returns DENY so the LLM sees
        the rejection and can adapt.
        """
        with self._lock:
            req_ids = list(self._by_trace.pop(trace_id, set()))
            slots = [self._slots.pop(r, None) for r in req_ids]
        for slot in slots:
            if slot is not None and not slot.event.is_set():
                slot.event.set()  # result stays None — signals "cancelled"


# ── HITL hook bridging to the registry ─────────────────────────────────────

class _RegistryConfirmHook(PreToolHook):
    """PreToolHook that opens a confirm slot and waits for the frontend.

    Bound at construction to a single (registry, on_event, trace_id) triple
    so concurrent chats can never cross-route their HITL events. Both the
    registry and the on_event callback come straight from chat_stream's
    local scope — nothing is read off the engine instance at run time.
    """

    def __init__(
        self,
        registry: "_ConfirmRegistry",
        on_event,
        trace_id: str,
        confirm_tools: set[str],
    ):
        self._registry = registry
        self._on_event = on_event
        self._trace_id = trace_id
        self.confirm_tools = confirm_tools
        self.reason = ""

    def run(self, name: str, args: dict) -> HookResult:
        # Non-HITL tools: short-circuit without creating a span so the
        # LangSmith UI stays free of empty hitl_confirm runs under every
        # tool:<name> parent.
        if name not in self.confirm_tools:
            return HookResult.SKIP
        return self._traced_confirm(name, args)

    @traceable(run_type="chain", name="hitl_confirm")
    def _traced_confirm(self, name: str, args: dict) -> HookResult:
        """HITL wait wrapped as a child span. Records tool, args, request_id,
        wait duration, and terminal result (allow/deny/timeout/cancelled)."""
        rt = get_current_run_tree()
        req_id, slot = self._registry.open(self._trace_id, name)
        start = time.time()

        if rt is not None:
            rt.add_metadata({
                "request_id":   req_id,
                "tool_name":    name,
                "tool_args":    args,
                "trace_id":     self._trace_id,
                "timeout_sec":  config.HITL_TIMEOUT,
                "preview":      _preview(name, args),
                "status":       "waiting",
            })

        if self._on_event:
            self._on_event({
                "type": "confirm_request",
                "request_id": req_id,
                "tool_name": name,
                "tool_args": args,
                "preview": _preview(name, args),
            })

        signaled = slot.event.wait(timeout=config.HITL_TIMEOUT)
        wait_ms = int((time.time() - start) * 1000)

        def _tag(outcome: str):
            if rt is not None:
                rt.add_metadata({"result": outcome, "wait_ms": wait_ms})

        if not signaled:
            self._registry.discard(req_id)
            self.reason = f"HITL timeout ({config.HITL_TIMEOUT}s)"
            _tag("timeout")
            raise AbortRound(self.reason)

        if slot.result is True:
            _tag("allow")
            return HookResult.ALLOW

        if slot.result is None:
            self.reason = "HITL cancelled (client disconnected)"
            _tag("cancelled")
            raise AbortRound(self.reason)

        self.reason = "User rejected"
        _tag("deny")
        return HookResult.DENY


# ── LangSmith parent span ──────────────────────────────────────────────────

def _is_uuid(s: Optional[str]) -> bool:
    """True when s parses as a UUID (accepts both hex and dashed forms)."""
    if not s:
        return False
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


@traceable(run_type="chain", name="agent_round")
def _traced_agent_round(messages, system, tracker, on_event):
    """One agent round as a LangSmith parent run.

    No-op wrapper when LANGSMITH_TRACING is off — the decorator short-circuits
    and agent_loop runs exactly as before. When on, every wrap_anthropic LLM
    call, every @traceable tool span, and every HITL confirm span nests under
    this parent automatically via LangSmith's contextvar run tree.
    """
    agent_loop(messages, system, tracker, on_event=on_event)


# ── Engine ─────────────────────────────────────────────────────────────────

class AgentEngine:
    """One per process (per uvicorn worker). Owns shared sub-systems only."""

    def __init__(self):
        self.skill_loader = SkillLoader(config.SKILLS_DIR)
        self.mcp = MCPManager()
        self._confirm_registry = _ConfirmRegistry()

        self._executor = ThreadPoolExecutor(
            max_workers=config.MAX_CONCURRENT_CHATS,
            thread_name_prefix="agent-loop",
        )

        # tools.py module-level wiring. SkillLoader and MCP are read-only and
        # safe to share across chats. Todo and HookManager are per-thread —
        # bound inside each agent thread by chat_stream's _run_sync.
        tools_mod.SKILL_LOADER = self.skill_loader
        tools_mod.MCP = self.mcp

        # MCP wiring
        mcp_cfg = config.resolve_mcp_config()
        if mcp_cfg.get("servers"):
            self.mcp.start(mcp_cfg)
            tools_mod.rebuild_tools()

        self._hitl_tools = validate_hitl(config.resolve_hitl())
        config.CONFIRM = bool(self._hitl_tools)

        # Auth wiring — build providers from auth.json, expose config to
        # tools.py via module-level binding so the tool middleware can
        # look up mcp_bindings per dispatch.
        self._auth_config = config.resolve_auth_config()
        build_providers(self._auth_config.providers)
        tools_mod.AUTH_CONFIG = self._auth_config

        self.system = build_system_prompt(self.skill_loader, mcp_manager=self.mcp)

    # ── meta ──
    @property
    def info(self) -> dict:
        """Self-description for the frontend agent picker.

        Returns only fields the frontend needs to render the picker and the
        chat UI. Container-internal paths and operational knobs stay private.
        """
        return {
            "agent_name":   config.AGENT_NAME,
            "model":        config.MODEL,
            "mcp_tools":    sorted(self.mcp.tool_names),
            "hitl_tools":   sorted(self._hitl_tools),
            "hitl_timeout": config.HITL_TIMEOUT,
        }

    def get_tools(self) -> list[str]:
        from .core.tools import TOOLS
        return [t["name"] for t in TOOLS]

    def get_skills(self) -> dict[str, str]:
        return {
            name: skill["meta"].get("description", "")
            for name, skill in self.skill_loader.skills.items()
        }

    def get_skill_content(self, name: str) -> str | None:
        if name in self.skill_loader.skills:
            return self.skill_loader.get_content(name)
        return None

    # ── HITL response from frontend ──
    def respond_confirm(self, request_id: str, allowed: bool) -> bool:
        return self._confirm_registry.resolve(request_id, allowed)

    # ── chat ──
    async def chat_stream(
        self,
        messages: list,
        trace_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        user: Optional[UserIdentity] = None,
    ) -> AsyncGenerator[EngineEvent, None]:
        """Run one agent round over the supplied message history.

        Args:
            messages: full conversation history including the new user turn
                      at the end. The engine mutates this list internally
                      (appends assistant + tool_result messages) but doesn't
                      persist anything — the frontend is the source of truth.
            trace_id: identifier for this in-flight stream. Used to scope
                      HITL confirm requests so SSE disconnects only cancel
                      this trace's pending confirms. Auto-generated if omitted.
            conversation_id: optional frontend-owned identifier that spans
                      multiple rounds of the same chat thread. Forwarded to
                      LangSmith as session_id metadata so the Threads view
                      groups all rounds of this conversation together.
            user:     the UserIdentity produced by the require_user dependency.
                      Re-bound to the auth ContextVar inside the worker thread
                      so downstream tools see it; ContextVar does not cross the
                      ThreadPoolExecutor boundary automatically.
        """
        if trace_id is None:
            trace_id = uuid.uuid4().hex

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[Optional[EngineEvent]] = asyncio.Queue(maxsize=1024)

        def on_event(raw: dict):
            evt = _build_event(raw)
            if evt is None:
                return
            try:
                loop.call_soon_threadsafe(queue.put_nowait, evt)
            except RuntimeError:
                pass  # loop closed (client gone)

        # Per-chat state — none of this leaks across requests. The confirm
        # hook closes over (registry, on_event, trace_id) directly so it
        # never reads anything off the engine instance at run time.
        hooks = HookManager()
        if self._hitl_tools:
            hooks.add(_RegistryConfirmHook(
                self._confirm_registry, on_event, trace_id, self._hitl_tools,
            ))
        todo = Todo()
        tracker = TokenTracker()

        # LangSmith metadata — session_id groups rounds into a Thread in the
        # LangSmith UI; trace_id pinpoints the single round; agent_name lets
        # us filter by container. No-op when LANGSMITH_TRACING is off.
        langsmith_extra = {
            "metadata": {
                "session_id":      conversation_id,
                "conversation_id": conversation_id,
                "trace_id":        trace_id,
                "agent_name":      config.AGENT_NAME,
                "model":           config.MODEL,
                "user_id":         user.user_id if user else None,
            },
            "run_id": trace_id if _is_uuid(trace_id) else None,
        }

        def _forward_device_prompt(prompt):
            """device_code provider → SSE. Pushes a device_flow_request
            event into the same queue the frontend is reading."""
            on_event({
                "type": "device_flow_request",
                "provider": prompt.provider,
                "verification_uri": prompt.verification_uri,
                "user_code": prompt.user_code,
                "expires_in": prompt.expires_in,
                "message": prompt.message,
            })

        def _run_sync():
            # ContextVar does not cross ThreadPoolExecutor.submit — re-bind
            # the UserIdentity on this thread so downstream auth-aware tools
            # see it via auth.current_user().
            user_token = set_current_user(user) if user is not None else None
            tools_mod.set_thread_hooks(hooks)
            tools_mod.set_thread_todo(todo)
            set_prompt_callback(_forward_device_prompt)
            try:
                _traced_agent_round(
                    messages, self.system, tracker, on_event,
                    langsmith_extra=langsmith_extra,
                )
            except Exception as e:
                logger.exception("agent_loop crashed")
                loop.call_soon_threadsafe(queue.put_nowait, Error(message=str(e)))
            finally:
                tools_mod.set_thread_hooks(None)
                tools_mod.set_thread_todo(None)
                set_prompt_callback(None)
                if user_token is not None:
                    reset_current_user(user_token)
                loop.call_soon_threadsafe(queue.put_nowait, None)

        future = loop.run_in_executor(self._executor, _run_sync)

        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        except asyncio.CancelledError:
            # SSE client disconnected. Wake up any HITL waits for this trace
            # so the agent thread can abort cleanly and release itself.
            self._confirm_registry.cancel_trace(trace_id)
            raise
        finally:
            await future

    # ── shutdown ──
    def shutdown(self):
        self.mcp.shutdown()
        self._executor.shutdown(wait=False)


# ── event factory ──────────────────────────────────────────────────────────

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
    "token_usage": lambda d: TokenUsage(turn=d.get("turn", {}), total=d.get("total", {}),
                                        cost=d.get("cost", "")),
    "status": lambda d: Status(message=d.get("message", "")),
    "done": lambda d: Done(stop_reason=d.get("stop_reason", "")),
    "confirm_request": lambda d: ConfirmRequest(
        request_id=d.get("request_id", ""),
        tool_name=d.get("tool_name", ""),
        tool_args=d.get("tool_args", {}),
        preview=d.get("preview", ""),
    ),
    "device_flow_request": lambda d: DeviceFlowRequest(
        provider=d.get("provider", ""),
        verification_uri=d.get("verification_uri", ""),
        user_code=d.get("user_code", ""),
        expires_in=int(d.get("expires_in", 0) or 0),
        message=d.get("message", ""),
    ),
}


def _build_event(raw: dict) -> Optional[EngineEvent]:
    factory = _EVENT_MAP.get(raw.get("type", ""))
    return factory(raw) if factory else None
