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
class Error(EngineEvent):
    type: str = "error"
    message: str = ""
