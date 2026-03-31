"""Rich streaming display — Live panels for thinking, tools, and response."""

import shutil

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from .formatters import format_tool_compact, format_token_line


class StreamState:
    """Accumulates engine events into display-ready state."""

    def __init__(self):
        self.thinking_text = ""
        self.thinking_active = False
        self.response_text = ""
        self.tool_calls: list[dict] = []
        self.token_usage = None
        self.is_done = False
        self.error: str | None = None

    def handle_event(self, event):
        match event.type:
            case "thinking_start":
                self.thinking_active = True
            case "thinking_delta":
                self.thinking_text += event.text
            case "thinking_stop":
                self.thinking_active = False
            case "text_delta":
                self.response_text += event.text
            case "text_stop":
                pass
            case "tool_call":
                self.tool_calls.append({
                    "id": event.id, "name": event.name,
                    "args_summary": event.args_summary,
                    "status": "running", "result": None, "is_error": False,
                })
            case "tool_result":
                for tc in self.tool_calls:
                    if tc["id"] == event.id:
                        tc["status"] = "error" if event.is_error else "success"
                        tc["result"] = event.output
            case "token_usage":
                self.token_usage = event
            case "done":
                self.is_done = True
            case "error":
                self.error = event.message


def compute_height_budget(terminal_height: int) -> dict:
    available = terminal_height - 4
    return {
        "thinking": max(3, int(available * 0.15)),
        "tools": max(3, int(available * 0.25)),
        "response": max(5, int(available * 0.60)),
    }


def _truncate_lines(text: str, max_lines: int) -> str:
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    return "...\n" + "\n".join(lines[-max_lines + 1:])


def create_streaming_display(state: StreamState) -> Group:
    """Build a Rich Group renderable for Live.update() during streaming."""
    term_h = shutil.get_terminal_size().lines
    budget = compute_height_budget(term_h)
    parts = []

    # Thinking panel
    if state.thinking_text:
        title_parts = ["thinking"]
        if state.thinking_active:
            title_parts.append("...")
        title = " ".join(title_parts)
        truncated = _truncate_lines(state.thinking_text, budget["thinking"])
        parts.append(Panel(
            Text(truncated, style="dim"),
            title=title, border_style="dim", padding=(0, 1),
        ))

    # Tool status lines
    if state.tool_calls:
        tool_lines = []
        for tc in state.tool_calls[-budget["tools"]:]:
            label = format_tool_compact(tc["name"], tc["args_summary"])
            badge = ("[ADF] ", "bold blue") if tc["name"].startswith("mcp_adf_") else ("", "")
            if tc["status"] == "running":
                tool_lines.append(Text.assemble(
                    ("● ", "yellow"), badge, (label, ""), (" ", ""), ("running...", "dim"),
                ))
            elif tc["status"] == "error":
                tool_lines.append(Text.assemble(("✗ ", "red"), badge, (label, "")))
                if tc["result"]:
                    preview = tc["result"][:200].split("\n")[0]
                    tool_lines.append(Text(f"  {preview}", style="dim red"))
            else:
                tool_lines.append(Text.assemble(("✓ ", "green"), badge, (label, "")))
                if tc["result"]:
                    preview_lines = tc["result"][:500].split("\n")[:3]
                    for line in preview_lines:
                        tool_lines.append(Text(f"  {line[:120]}", style="dim"))
        parts.extend(tool_lines)

    # Response
    if state.response_text:
        try:
            parts.append(Markdown(state.response_text))
        except Exception:
            parts.append(Text(state.response_text))

    # Error
    if state.error:
        parts.append(Text(f"Error: {state.error}", style="bold red"))

    return Group(*parts) if parts else Group(Text("..."))


def display_final(state: StreamState, console):
    """Print static final output after streaming ends."""
    # Thinking (collapsed)
    if state.thinking_text:
        lines = state.thinking_text.split("\n")
        n = len(lines)
        if n <= 6:
            body = state.thinking_text
        else:
            head = "\n".join(lines[:3])
            tail = "\n".join(lines[-3:])
            body = f"{head}\n  [{n - 6} lines hidden]\n{tail}"
        console.print(Panel(
            Text(body, style="dim"), title=f"thinking ({n} lines)",
            border_style="dim", padding=(0, 1),
        ))

    # Tool calls
    for tc in state.tool_calls:
        label = format_tool_compact(tc["name"], tc["args_summary"])
        badge = ("[ADF] ", "bold blue") if tc["name"].startswith("mcp_adf_") else ("", "")
        if tc["is_error"]:
            console.print(Text.assemble(("✗ ", "red"), badge, (label, "")))
        else:
            console.print(Text.assemble(("✓ ", "green"), badge, (label, "")))
        if tc["result"]:
            preview_lines = tc["result"][:1000].split("\n")[:5]
            for line in preview_lines:
                console.print(Text(f"  {line[:150]}", style="dim"))

    # Response
    if state.response_text:
        console.print(Markdown(state.response_text))

    # Token usage
    if state.token_usage:
        token_str = format_token_line(state.token_usage)
        if token_str:
            console.print(Text(token_str, style="dim"), justify="right")
