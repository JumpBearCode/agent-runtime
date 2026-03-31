# Prompt 2: Rich CLI Frontend

## Task
Build a Rich CLI frontend consuming AgentEngine events

## Context
`agent_frontend/engine.py` provides `AgentEngine` with async `chat_stream()` yielding
`EngineEvent` dataclasses. Build a Rich-based CLI that consumes these events.

Reference: study `/Users/wqeq/Desktop/project/agent-runtime/ADFAgent/adf_agent/cli.py`
for Rich display patterns (Live, Panel, Spinner, Markdown, height budgeting).
Do NOT copy its LangChain agent code — only adapt the display/UX patterns.

## Files to Create

```
agent_frontend/
├── cli/
│   ├── __init__.py
│   ├── app.py          # Entry point: argparse, REPL, event consumer
│   ├── display.py      # Rich Live display, panels, streaming layout
│   └── formatters.py   # Tool result formatting, token display
```

## app.py — Entry Point

### argparse flags
Same flags as agent_runtime/__main__.py, mapping to EngineConfig:
- `--workspace / -w` (default None)
- `--thinking / -t` (store_true)
- `--thinking-budget` (int, default 10000)
- `--mcp-config` (str)
- `--confirm` (store_true)
- `--keep-sandbox` (store_true)
- `--session / -s` (str, resume session)
- `--list-sessions` (store_true, list and exit)

### main() function
1. Parse args, construct EngineConfig, instantiate AgentEngine
2. Print startup banner (Rich Panel with engine.startup_info)
3. Session: resume with --session or create new
4. Enter REPL loop

### REPL loop (cmd_interactive)
Use `prompt_toolkit.PromptSession` with:
- `HTML` prompt: `<cyan>agent >> </cyan>`
- `FileHistory("~/.agent_frontend_history")`
- `AutoSuggestFromHistory()`
- Multiline: support backslash continuation (same logic as agent_runtime/__main__.py read_input)

Commands:
- `/compact` → call engine.compact(session_id), print "[compacted]"
- `/todo` → print engine.get_todo()
- `/tools` → print formatted tool list
- `/sessions` → print session list as Rich Table
- `exit` / `quit` / empty → break

For user messages: run async event loop to consume chat_stream:
```python
import asyncio
async def _stream_turn(engine, session_id, query, console):
    state = StreamState()
    with Live(console=console, refresh_per_second=12, transient=True) as live:
        async for event in engine.chat_stream(session_id, query):
            state.handle_event(event)
            live.update(create_streaming_display(state))
    display_final(state, console)

# In the sync REPL:
asyncio.run(_stream_turn(engine, session_id, query, console))
```

Handle KeyboardInterrupt during streaming → print "[interrupted]", continue REPL.
Cleanup: `engine.shutdown()` in finally block.

## display.py — Rich Streaming Display

### StreamState class
Accumulates events into display-ready state:

```python
class StreamState:
    thinking_text: str = ""
    thinking_active: bool = False
    response_text: str = ""
    tool_calls: list[dict] = []     # {id, name, args_summary, status, result, is_error}
    tool_results: dict = {}         # tool_id → result mapping
    token_usage: dict | None = None
    is_done: bool = False
    error: str | None = None

    def handle_event(self, event: EngineEvent):
        match event.type:
            case "thinking_start": self.thinking_active = True
            case "thinking_delta": self.thinking_text += event.text
            case "thinking_stop": self.thinking_active = False
            case "text_delta": self.response_text += event.text
            case "text_stop": pass
            case "tool_call":
                self.tool_calls.append({
                    "id": event.id, "name": event.name,
                    "args_summary": event.args_summary,
                    "status": "running", "result": None, "is_error": False
                })
            case "tool_result":
                for tc in self.tool_calls:
                    if tc["id"] == event.id:
                        tc["status"] = "error" if event.is_error else "success"
                        tc["result"] = event.output
            case "token_usage": self.token_usage = event
            case "done": self.is_done = True
            case "error": self.error = event.message
```

### create_streaming_display(state) → Rich Group
Returns a Group of renderables for Live.update():

1. **Thinking Panel** (only if thinking_text exists):
   - `Panel(thinking_text[-1500:], title="thinking..." + Spinner("dots") if active,
     border_style="dim", style="dim")`
   - Max height: 15% of terminal

2. **Tool Status Lines** (only if tool_calls exist):
   - Each tool: `● ToolName(args)` green=success, yellow=running+spinner, red=error
   - Running tools show `Spinner("dots")` inline
   - Completed tools show truncated result (first 3 lines, dim)
   - Max height: 25% of terminal

3. **Response Panel** (main content):
   - `Markdown(response_text)` rendered via Rich
   - No Panel border during streaming (clean look)
   - Gets remaining terminal height

### display_final(state, console)
After streaming ends, print static output:

1. **Thinking** (if exists): Collapsed — show first 3 + last 3 lines in dim Panel,
   "[N lines hidden]" in middle. Title: "thinking (N lines)"

2. **Tool calls**: Each as a compact line:
   `✓ Bash(git status)` or `✗ ReadFile(missing.py)` with result preview (max 5 lines, dim)

3. **Response**: `console.print(Markdown(state.response_text))`

4. **Token usage**: Right-aligned dim line:
   `in:12,340 out:487 cached:8,200 $0.0234`

### compute_height_budget(terminal_height) → dict
```python
available = terminal_height - 4  # reserve for prompt + status
return {
    "thinking": max(3, int(available * 0.15)),
    "tools": max(3, int(available * 0.25)),
    "response": max(5, int(available * 0.60)),
}
```

## formatters.py

```python
def format_tool_compact(name: str, args_summary: str) -> str:
    """e.g. 'Bash($ git status)' or 'ReadFile(config.py)'"""

def format_token_line(usage: TokenUsage) -> str:
    """e.g. 'in:12,340 out:487 cached:8,200 $0.0234'"""

def format_session_table(sessions: list[dict]) -> Table:
    """Rich Table: ID | Created | Messages | Size"""
```

## Verification Steps
- [ ] `uv sync --extra cli` installs rich and prompt-toolkit
- [ ] `uv run agent-cli --help` shows all flags
- [ ] `uv run agent-cli --list-sessions` lists sessions (or shows empty)
- [ ] `uv run agent-cli -w .` launches interactive REPL with Rich banner
- [ ] Sending a message shows:
  - Thinking in dim panel (if --thinking enabled)
  - Tool calls with yellow ● spinner → green ● on completion
  - Response as rendered markdown
  - Token usage line after response
- [ ] `/todo` command prints current todo list
- [ ] `/compact` command compresses context
- [ ] `/tools` command lists available tools
- [ ] Ctrl+C during streaming shows "[interrupted]" and returns to prompt
- [ ] `--session <id>` resumes an existing session
- [ ] Exiting shows total token usage summary
- [ ] The original `uv run agent` (agent_runtime.__main__) still works

## What NOT to do
- Do NOT modify agent_runtime/ or agent_frontend/engine.py
- Do NOT use LangChain — consume events from engine.chat_stream()
- Do NOT add web server code in the CLI package

After all verification steps pass, output: <promise>COMPLETE</promise>
