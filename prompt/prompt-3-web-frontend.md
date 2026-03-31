# Prompt 3: Web Frontend (FastAPI + SSE + chatui-sso Style)

## Task
Build a web frontend with FastAPI backend and SSE streaming

## Context
`agent_frontend/engine.py` provides `AgentEngine` with async `chat_stream()` yielding
events. Build a web UI that replicates the visual style of the chatui-sso project.

You MUST read these reference files first:
- `/Users/wqeq/Desktop/project/chatui-sso/app/static/index.html` — layout structure
- `/Users/wqeq/Desktop/project/chatui-sso/app/static/styles.css` — visual style to replicate
- `/Users/wqeq/Desktop/project/chatui-sso/app/static/script.js` — interaction patterns

Replicate the look and feel (sidebar, chat layout, colors, typography, animations) but
adapt for our agent runtime: add thinking visualization, tool call display, SSE streaming,
and token usage. No OAuth — this is a local app.

## Files to Create

```
agent_frontend/
├── web/
│   ├── __init__.py
│   ├── server.py       # FastAPI app, API routes
│   ├── run.py           # uvicorn launcher with argparse
│   └── static/
│       ├── index.html   # SPA — sidebar + chat canvas
│       ├── styles.css   # chatui-sso visual style + agent extensions
│       └── script.js    # SSE client, event handling, thinking UI
```

## server.py — FastAPI Backend

```python
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse
from pathlib import Path
from .engine_instance import get_engine  # see below

app = FastAPI(title="Agent Frontend")
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
```

### Routes:

**GET /** → serve index.html

**GET /api/config** → `engine.startup_info` (model, workspace, tools count, etc.)

**GET /api/sessions** → `engine.list_sessions()`

**POST /api/sessions** → `engine.create_session()` → return `{"id": session_id}`

**GET /api/sessions/{id}** → load session, return `{"id": id, "messages": history}`
- Convert internal message format to frontend-friendly format:
  - User messages: `{"role": "user", "content": text}`
  - Assistant messages: `{"role": "assistant", "content": text, "thinking": text, "tool_calls": [...]}`

**DELETE /api/sessions/{id}** → delete session file

**POST /api/sessions/{id}/chat** → SSE streaming endpoint
```python
@app.post("/api/sessions/{session_id}/chat")
async def chat(session_id: str, request: Request):
    body = await request.json()
    message = body["message"]
    engine = get_engine()

    async def event_stream():
        async for event in engine.chat_stream(session_id, message):
            yield {"event": event.type, "data": json.dumps(event.to_dict(), ensure_ascii=False)}

    return EventSourceResponse(event_stream())
```

**GET /api/tools** → `engine.get_tools()`

**GET /api/skills** → `engine.get_skills()`

### engine_instance.py (or inline in server.py)
Singleton pattern — engine initialized once on startup:
```python
_engine = None
def get_engine():
    global _engine
    if _engine is None:
        from ..engine import AgentEngine, EngineConfig
        _engine = AgentEngine(EngineConfig(
            workspace=os.environ.get("AGENT_WORKSPACE", "."),
            thinking="AGENT_THINKING" in os.environ,
            # ... read from env vars
        ))
    return _engine
```

## run.py — Entry Point

```python
import argparse, os, uvicorn

def main():
    parser = argparse.ArgumentParser(description="Agent Frontend Web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workspace", "-w", default=".")
    parser.add_argument("--thinking", "-t", action="store_true")
    parser.add_argument("--thinking-budget", type=int, default=10000)
    parser.add_argument("--mcp-config", default=None)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    # Pass config via env vars to server module
    os.environ["AGENT_WORKSPACE"] = args.workspace or "."
    if args.thinking:
        os.environ["AGENT_THINKING"] = "1"
        os.environ["AGENT_THINKING_BUDGET"] = str(args.thinking_budget)
    if args.mcp_config:
        os.environ["AGENT_MCP_CONFIG"] = args.mcp_config

    uvicorn.run("agent_frontend.web.server:app",
                host=args.host, port=args.port, reload=False)
```

## index.html — SPA Layout

Replicate chatui-sso structure exactly:

- **Sidebar** (left, 260px, collapsible):
  - Brand logo area + sidebar toggle button (same SVG icons as chatui-sso)
  - "New chat" button (same pencil icon)
  - Session list (same `<details>` collapsible "Chats" group)
  - Each session item: title, 3-dot menu with rename/delete
  - Bottom: "Local Agent" user profile (no auth)

- **Main area** (right):
  - **Top bar**: Sidebar toggle + model info display (show engine config model name,
    NOT a selector — we only have one model)
  - **Chat canvas** (`#chat-canvas`): Scrollable message area
  - **Input area**: Sticky bottom, textarea with send button (same styling as chatui-sso)

Key differences from chatui-sso:
- No model selector dropdown (single model from config)
- No debug headers section
- Add thinking blocks inside assistant messages
- Add tool call blocks inside assistant messages
- Add token usage footer after each assistant message

## styles.css — Visual Style

Copy the chatui-sso design system wholesale:
- Same sidebar colors (#f9f9f9), widths (260px/60px), transitions
- Same message bubbles (user: #f4f4f4, pill-shaped; assistant: transparent, left-aligned)
- Same font stack (ui-sans-serif, -apple-system, system-ui, ...)
- Same input area styling (#f4f4f4, border-radius 26px)
- Same send button (black circle, white arrow)
- Same sidebar toggle animation with checkbox

ADD these agent-specific styles:

### Thinking blocks (blurred exposure):
```css
.thinking-block {
    position: relative;
    background: linear-gradient(135deg, #f5f5f5, #ebebeb);
    border-left: 3px solid #c8c8c8;
    border-radius: 8px;
    padding: 0.75rem 1rem;
    margin: 0.5rem 0;
    font-size: 0.82rem;
    line-height: 1.6;
    color: #999;
    max-height: 100px;
    overflow: hidden;
    cursor: pointer;
    transition: all 0.3s ease;
    /* The blur effect */
    filter: blur(2px);
    -webkit-filter: blur(2px);
}
.thinking-block::before {
    content: "thinking...";
    display: block;
    font-size: 0.72rem;
    font-weight: 600;
    color: #aaa;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    margin-bottom: 0.25rem;
    /* Label is always sharp */
    filter: blur(0) !important;
    position: relative;
    z-index: 1;
}
/* Hover: un-blur and expand */
.thinking-block:hover,
.thinking-block.expanded {
    filter: blur(0);
    -webkit-filter: blur(0);
    max-height: 600px;
    color: #666;
    overflow-y: auto;
}
/* During streaming: show a pulsing dot */
.thinking-block.streaming::after {
    content: "●";
    animation: pulse 1s ease-in-out infinite;
    color: #999;
    margin-left: 4px;
}
@keyframes pulse {
    0%, 100% { opacity: 0.3; }
    50% { opacity: 1; }
}
```

### Tool call blocks:
```css
.tool-call-block {
    background: #f8f9fa;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 0.5rem 0.75rem;
    margin: 0.4rem 0;
    font-family: 'SF Mono', SFMono-Regular, Consolas, monospace;
    font-size: 0.8rem;
}
.tool-call-block .tool-header {
    display: flex;
    align-items: center;
    gap: 6px;
}
.tool-call-block .tool-status {
    width: 8px; height: 8px;
    border-radius: 50%;
    display: inline-block;
}
.tool-status.running { background: #eab308; animation: pulse 1s infinite; }
.tool-status.success { background: #22c55e; }
.tool-status.error { background: #ef4444; }
.tool-call-block .tool-name { font-weight: 600; color: #4a5568; }
.tool-call-block .tool-args { color: #718096; margin-left: 4px; }
.tool-call-block .tool-result {
    margin-top: 0.35rem;
    padding-top: 0.35rem;
    border-top: 1px solid #e2e8f0;
    white-space: pre-wrap;
    font-size: 0.75rem;
    color: #64748b;
    max-height: 150px;
    overflow-y: auto;
    display: none;  /* collapsed by default */
}
.tool-call-block.has-result .tool-result { display: block; }
.tool-call-block .toggle-result {
    font-size: 0.7rem; color: #94a3b8; cursor: pointer;
    margin-left: auto;
}
```

### Token usage:
```css
.token-usage {
    font-size: 0.72rem;
    color: #b0b0b0;
    text-align: right;
    margin-top: 0.5rem;
    font-family: 'SF Mono', monospace;
}
```

### Streaming cursor:
```css
.streaming-cursor::after {
    content: "▊";
    animation: blink 0.8s ease-in-out infinite;
    color: #333;
}
@keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0; }
}
```

## script.js — Frontend Logic

### State
```javascript
let sessions = [];
let currentSessionId = null;
let currentStreamController = null;  // AbortController for stopping streams
```

### Session Management
- `loadSessions()` → GET /api/sessions, render sidebar
- `createSession()` → POST /api/sessions, switch to new session
- `selectSession(id)` → GET /api/sessions/{id}, render messages
- `deleteSession(id)` → confirm, DELETE /api/sessions/{id}
- `renameSession(id)` → inline edit (same pattern as chatui-sso)

### Message Rendering
For historical messages (from session load):
```javascript
function renderMessage(msg) {
    if (msg.role === 'user') {
        return `<div class="message user-message">
            <div class="message-bubble">${escapeHtml(msg.content)}</div>
        </div>`;
    }
    // Assistant: may have thinking + tool_calls + content
    let html = '<div class="message assistant-message">';
    if (msg.thinking) {
        html += `<div class="thinking-block">${escapeHtml(msg.thinking)}</div>`;
    }
    if (msg.tool_calls) {
        for (const tc of msg.tool_calls) {
            html += renderToolCall(tc);
        }
    }
    html += `<div class="response-content">${renderMarkdown(msg.content)}</div>`;
    if (msg.token_usage) {
        html += `<div class="token-usage">${msg.token_usage}</div>`;
    }
    html += '</div>';
    return html;
}
```

### SSE Streaming (core function)
```javascript
async function sendMessage(sessionId, message) {
    // 1. Append user bubble immediately
    appendUserMessage(message);
    clearInput();
    disableInput();

    // 2. Create assistant message container
    const assistantEl = createAssistantMessageEl();
    const thinkingEl = createThinkingEl(assistantEl);   // hidden initially
    const responseEl = createResponseEl(assistantEl);
    const toolsContainer = createToolsContainer(assistantEl);

    // 3. SSE via fetch + ReadableStream (NOT EventSource, since we POST)
    currentStreamController = new AbortController();

    try {
        const response = await fetch(`/api/sessions/${sessionId}/chat`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message}),
            signal: currentStreamController.signal
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const {done, value} = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, {stream: true});
            const events = parseSSE(buffer);
            buffer = events.remaining;

            for (const event of events.parsed) {
                handleStreamEvent(event, {thinkingEl, responseEl, toolsContainer, assistantEl});
            }
        }
    } catch (e) {
        if (e.name !== 'AbortError') {
            appendErrorMessage(assistantEl, e.message);
        }
    } finally {
        enableInput();
        currentStreamController = null;
        // Finalize: remove streaming cursors, collapse thinking
        finalizeAssistantMessage(assistantEl);
        scrollToBottom();
    }
}
```

### SSE Parser
```javascript
function parseSSE(buffer) {
    const events = [];
    const lines = buffer.split('\n');
    let remaining = '';
    let currentEvent = null;
    let currentData = '';

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
            currentData = line.slice(6);
        } else if (line === '' && currentEvent) {
            try {
                events.push({type: currentEvent, data: JSON.parse(currentData)});
            } catch(e) {}
            currentEvent = null;
            currentData = '';
        }
    }
    // Keep incomplete event in buffer
    if (currentEvent) {
        remaining = `event: ${currentEvent}\n`;
        if (currentData) remaining += `data: ${currentData}\n`;
    }
    return {parsed: events, remaining};
}
```

### Event Handler
```javascript
function handleStreamEvent(event, els) {
    switch (event.type) {
        case 'thinking_start':
            els.thinkingEl.style.display = 'block';
            els.thinkingEl.classList.add('streaming');
            break;

        case 'thinking_delta':
            appendText(els.thinkingEl, event.data.text);
            break;

        case 'thinking_stop':
            els.thinkingEl.classList.remove('streaming');
            break;

        case 'text_delta':
            appendMarkdown(els.responseEl, event.data.text);
            scrollToBottom();
            break;

        case 'tool_call':
            addToolCallEl(els.toolsContainer, event.data);
            break;

        case 'tool_result':
            updateToolResultEl(els.toolsContainer, event.data);
            break;

        case 'token_usage':
            setTokenUsage(els.assistantEl, event.data);
            break;

        case 'done':
            break;

        case 'error':
            appendErrorMessage(els.assistantEl, event.data.message);
            break;
    }
}
```

### Markdown Rendering
Include via CDN (add to index.html `<head>`):
```html
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11/build/styles/github.min.css">
<script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11/build/highlight.min.js"></script>
```

Configure marked to use highlight.js:
```javascript
marked.setOptions({
    highlight: function(code, lang) {
        if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, {language: lang}).value;
        }
        return hljs.highlightAuto(code).value;
    },
    breaks: true,
});
```

Progressive markdown: accumulate raw text, re-render full markdown on each text_delta
(debounce to every 50ms for performance).

### Input Area
- Textarea with auto-grow (same as chatui-sso)
- Enter to send, Shift+Enter for newline
- Disable during streaming, show "Stop" button that calls `currentStreamController.abort()`
- Re-enable after stream completes

## Verification Steps
- [ ] `uv sync --extra web` installs fastapi, uvicorn, sse-starlette
- [ ] `uv run agent-web --help` shows all flags
- [ ] `uv run agent-web -w .` starts server on localhost:8080
- [ ] Browser at http://localhost:8080 shows chat UI with sidebar
- [ ] Sidebar matches chatui-sso visual style (colors, fonts, spacing, transitions)
- [ ] "New chat" creates a session (appears in sidebar list)
- [ ] Sending "Hello" streams response progressively:
  - Text appears word-by-word with blinking cursor
  - If --thinking: thinking block appears blurred, un-blurs on hover
  - Tool calls show with yellow dot → green dot on completion
  - Token usage appears after response
- [ ] Clicking a session in sidebar loads its history with correct formatting
- [ ] Delete session removes it from sidebar
- [ ] Sidebar collapse/expand animates smoothly
- [ ] "Stop" button during streaming aborts the request
- [ ] No JavaScript console errors during normal usage
- [ ] The original `uv run agent` and `uv run agent-cli` still work

## What NOT to do
- Do NOT add React/Vue/Angular — plain HTML/CSS/JS only
- Do NOT add OAuth, login pages, or user management
- Do NOT use WebSocket — use SSE (simpler, one-directional streaming is sufficient)
- Do NOT use Postgres or Redis — sessions via engine's SessionStore (JSONL)
- Do NOT modify agent_runtime/ or agent_frontend/engine.py
- Do NOT bundle node_modules — CDN only for marked.js and highlight.js

After all verification steps pass, output: <promise>COMPLETE</promise>
