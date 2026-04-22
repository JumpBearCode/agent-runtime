"""SSE event schemas exchanged with the chat client."""

from dataclasses import dataclass, asdict, field
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
    args: dict = field(default_factory=dict)
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
    turn: dict = field(default_factory=dict)
    total: dict = field(default_factory=dict)
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
class ConfirmRequest(EngineEvent):
    """Sent over SSE when a HITL-gated tool needs approval.

    The client must POST /api/confirm/{request_id} with {"allowed": bool}
    within HITL_TIMEOUT seconds, otherwise the round is aborted.
    """
    type: str = "confirm_request"
    request_id: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    preview: str = ""


@dataclass
class DeviceFlowRequest(EngineEvent):
    """Sent over SSE when a device_code auth provider needs the user to log in.

    Unlike ConfirmRequest, no callback is expected — the runtime is
    already polling the IdP in the background. Frontend should open the
    verification URL in a new tab (or show a modal with URL+user_code)
    so the user can complete login.
    """
    type: str = "device_flow_request"
    provider:         str = ""
    verification_uri: str = ""
    user_code:        str = ""
    expires_in:       int = 0
    message:          str = ""


@dataclass
class Error(EngineEvent):
    type: str = "error"
    message: str = ""
