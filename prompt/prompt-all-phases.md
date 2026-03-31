# Agent Frontend — All Phases (Self-Detecting)

You are building `agent_frontend`, a dual-frontend (Rich CLI + Web) for the existing `agent_runtime`.
This prompt covers ALL phases. On each iteration, detect where you are and continue from there.

## Phase Detection

Run these checks at the START of every iteration to determine current phase:

```
Phase 1 DONE if: agent_frontend/engine.py exists AND agent_frontend/schemas.py exists
                  AND `uv sync --all-extras` succeeds
                  AND agent_runtime/loop.py contains "on_event"

Phase 2 DONE if: Phase 1 DONE AND agent_frontend/cli/app.py exists
                  AND `uv run agent-cli --help` exits 0

Phase 3 DONE if: Phase 1 DONE AND agent_frontend/web/server.py exists
                  AND agent_frontend/web/static/index.html exists
                  AND `uv run agent-web --help` exits 0

Phase 4 DONE if: Phase 2+3 DONE AND mcp.json exists AND skills/ dir has 3 skills
                  AND tests/test_integration.py exists
```

If a phase is DONE but broken (imports fail, tests fail), fix it before moving on.

---

## Phase 1: Foundation — uv workspace + loop.py callback + engine

READ `prompt/prompt-1-foundation.md` for full details. Summary:

1. **uv workspace**: Add `[tool.uv.workspace] members = ["agent_frontend"]` to root pyproject.toml.
   Create `agent_frontend/pyproject.toml` with deps on `agent-runtime`, optional-deps for `[cli]` (rich, prompt-toolkit) and `[web]` (fastapi, uvicorn, sse-starlette).
   Entry points: `agent-cli` and `agent-web`.

2. **loop.py callback**: Add `on_event=None` param to `_stream_response()` and `agent_loop()`.
   Emit events alongside existing prints: text_delta, thinking_delta, thinking_start/stop,
   tool_call, tool_result, token_usage, status, done. Keep ALL existing stdout behavior.
   ~30 lines added, fully backward compatible.

3. **engine.py + schemas.py**: AgentEngine class wraps agent_runtime init (mirrors __main__.py).
   `async chat_stream()` runs agent_loop in ThreadPoolExecutor, bridges events via
   `asyncio.Queue` using `loop.call_soon_threadsafe()`. EngineEvent dataclasses with `to_sse()`.

**Verify**: `uv sync --all-extras` works. `python -m agent_runtime` still works. Engine smoke test passes.

---

## Phase 2: Rich CLI Frontend

READ `prompt/prompt-2-rich-cli.md` for full details. Summary:

Create `agent_frontend/cli/` with `app.py`, `display.py`, `formatters.py`.

- **app.py**: argparse (same flags as agent_runtime), REPL with prompt_toolkit, commands: /compact /todo /tools /sessions.
  Consumes `engine.chat_stream()` via `asyncio.run()`.
- **display.py**: `StreamState` accumulates events. `create_streaming_display()` returns Rich Group
  (thinking panel dim, tool status with spinners, response as Markdown). `display_final()` for static output.
  `compute_height_budget()` for terminal-aware layout.
- **formatters.py**: `format_tool_compact()`, `format_token_line()`, `format_session_table()`.

Reference: study `ADFAgent/adf_agent/cli.py` for Rich patterns (Live, Panel, Spinner, height budgeting).

**Verify**: `uv run agent-cli --help` works. Interactive REPL streams events with Rich display.

---

## Phase 3: Web Frontend (FastAPI + SSE)

READ `prompt/prompt-3-web-frontend.md` for full details. Summary:

Create `agent_frontend/web/` with `server.py`, `run.py`, `static/{index.html,styles.css,script.js}`.

- **server.py**: FastAPI app. Routes: GET /, GET/POST/DELETE /api/sessions, POST /api/sessions/{id}/chat (SSE via EventSourceResponse), GET /api/tools, /api/skills, /api/config.
- **run.py**: argparse + uvicorn launcher, passes config via env vars.
- **Visual style**: READ and replicate `/Users/wqeq/Desktop/project/chatui-sso/app/static/` —
  same sidebar (#f9f9f9, 260px, collapsible), same message bubbles, same fonts, same input area.
- **Agent additions**: Thinking blocks with CSS `filter: blur(2px)`, un-blur on hover.
  Tool calls with status dots (yellow=running, green=success, red=error). Token usage footer.
- **SSE streaming**: fetch + ReadableStream (not EventSource, since POST). Parse SSE events,
  handle text_delta/thinking_delta/tool_call/tool_result/token_usage/done.
- **Markdown**: CDN marked.js + highlight.js. Progressive rendering debounced at 50ms.

**Verify**: `uv run agent-web -w .` serves UI at localhost:8080. Streaming works. Thinking blurs.

---

## Phase 4: ADF MCP + Skills Integration

READ `prompt/prompt-4-adf-integration.md` for full details. Summary:

1. **mcp.json** at repo root: `{"servers": {"adf": {"type": "stdio", "command": "uv", "args": ["run", "python", "adf_mcp_server.py"]}}}`.
2. **skills/** dir with 3 SKILL.md files adapted from ADFAgent: `test-linked-service`, `find-pipelines-by-service`, `adf-overview` (new). Replace ADFAgent tool names with `mcp_adf_*` equivalents.
3. **CLI enhancements**: MCP info in banner, `/tools` groups MCP tools, `/skills` command, `[ADF]` badge on mcp_adf_ tool calls.
4. **Web enhancements**: Azure blue accent (border-left: #0078d4) on ADF tool call blocks.
5. **tests/test_integration.py**: Verify skills load, MCP tools discovered (skip if no Azure creds).

**Verify**: Skills load. CLI/Web show MCP tools. Tests pass.

---

## Global Rules (ALL Phases)

- **Do NOT delete** agent_runtime/__main__.py — keep it working as fallback
- **Do NOT modify** any agent_runtime file EXCEPT loop.py (Phase 1 only, ~30 lines)
- **uv only** — no pip, no npm
- **No OAuth, no Postgres, no Redis** — local JSONL sessions
- **Web frontend**: plain HTML/CSS/JS, no React/Vue/Angular, CDN only for marked.js/highlight.js
- After EACH phase, verify the previous phases still work (no regressions)

## Completion

ALL phases are done when:
- [ ] `uv sync --all-extras` succeeds
- [ ] `uv run agent` launches original REPL (backward compat)
- [ ] `uv run agent-cli -w .` launches Rich CLI with streaming display
- [ ] `uv run agent-web -w .` serves web UI at localhost:8080 with SSE streaming
- [ ] skills/ has 3 SKILL.md files
- [ ] mcp.json exists
- [ ] `uv run pytest tests/test_integration.py -v` passes

When ALL checks pass: <promise>COMPLETE</promise>
