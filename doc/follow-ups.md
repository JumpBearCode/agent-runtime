# Follow-ups after stateless refactor

Captured 2026-04-20, after deleting `core/session.py` and reshaping `POST /api/chat` to take `{messages, trace_id}`. Items below are real residual issues found in a code walk; ordered by how likely they are to bite once a real frontend connects.

---

## 🔴 Critical — real bugs in stateless mode

These are leftovers from when the engine was per-session-stateful. They produce wrong data, not crashes, so they're easy to miss until users complain.

### 1. `tools_mod.TODO` cross-chat pollution

**Where**: `agent_runtime/engine.py:198` sets `tools_mod.TODO = Todo()` once during engine init. `core/tools.py` reads this module global from every `todo_write` / `todo_read` dispatch.

**Symptom**: chat A writes a todo list. Chat B (different user, different request) calls `todo_read` → sees A's list.

**Fix**: same pattern as `HOOKS` — add `_thread_state.todo` in `core/tools.py`, expose `set_thread_todo(todo)`, change `TOOL_HANDLERS["todo_*"]` lambdas to read `_active_todo()`. Engine binds a fresh `Todo()` per `_run_sync()` call.

### 2. `self.tracker` cross-chat pollution

**Where**: `agent_runtime/engine.py:191` makes `self.tracker = TokenTracker()` once. Every `chat_stream` passes the same tracker into `agent_loop`.

**Symptom**: the `token_usage.total` field in the SSE stream is "every token spent by every user since uvicorn started," not this round's running total. Frontend's per-conversation cost display will be junk.

**Fix**: drop `self.tracker` from `__init__`, create a local `tracker = TokenTracker()` inside `chat_stream` and pass that. Engine no longer holds tracker state. (Optionally keep a separate engine-level `_lifetime_tracker` for an admin/metrics endpoint, but don't put it in chat events.)

### 3. `auto_compact` writes a transcript file to disk

**Where**: `core/compression.py:60-66`:

```python
transcript_dir = config.WORKDIR / ".transcripts"
transcript_dir.mkdir(exist_ok=True)
transcript_path = transcript_dir / f"transcript_{int(time.time())}.jsonl"
with open(transcript_path, "w") as f:
    for msg in messages:
        f.write(json.dumps(msg, default=str) + "\n")
```

**Symptom**: every auto-compaction silently writes to container-local disk. Disk fills over time; container restart loses everything anyway.

**Fix**: delete the four lines (and the `print(...)` line that follows). If you want debugging visibility, replace with `logger.debug("auto_compact: %d messages", len(messages))`.

### 4. `should_compact` reads the polluted tracker

**Where**: `core/compression.py:9-19`. Looks at `tracker._turns[-1]` to decide whether to compact.

**Symptom**: because the tracker is shared (#2), a brand-new chat may compact on its first turn just because a *previous* chat already accumulated tokens.

**Fix**: solved automatically once #2 is fixed (per-request tracker is empty at the start of each chat).

---

## 🟡 Important — old debt worth clearing soon

### 5. `agent_loop` is full of `print(...)` + ANSI escapes

**Where**: `core/loop.py` — at least 8 print sites with codes like `\033[33m`, `\033[2m`, `\033[0m`.

**Why care**: in a container these go to stdout → docker logs / k8s log driver. ANSI codes pollute structured log shipping (Datadog, CloudWatch, Loki). Search/alerting becomes painful.

**Fix**: replace every `print(...)` with `logger.info(...)` / `logger.debug(...)`. Strip ANSI codes. One sed pass + a logging config in `api/app.py` (basic `logging.basicConfig(level=INFO, format=...)`).

### 6. `compression.auto_compact` uses sync Anthropic client

**Where**: `core/compression.py:70` calls `config.client.messages.create(...)`. Runs on the agent thread, so it blocks that thread (not the FastAPI loop) — fine for the half-plan but a future blocker for any "go full async" upgrade. Tag as TODO; not urgent today.

### 7. `agent_frontend/` is broken (placeholder)

It still imports `from agent_runtime.config` etc. — those moved to `agent_runtime.core.config`. Today `pyproject.toml` lists it under `[tool.uv.workspace] members = ["agent_frontend"]`, so `uv sync` installs it; importing anything inside it raises `ModuleNotFoundError`.

**Options**:
- `git rm -r agent_frontend/` and remove the workspace member entry. (You said you're rewriting the frontend — keeping legacy code is just confusion.)
- Or, keep but neutralize: empty out the `__init__.py`, drop from workspace members.

### 8. `tests/test_integration.py` is broken

Hasn't been touched since the restructure. Likely imports from old paths.

**Options**: rewrite as an integration test against the new `POST /api/chat` (boot uvicorn in a fixture, hit the endpoint, assert SSE events). Or `git rm` it for now.

### 9. CORS `*` + no auth

**Where**: `agent_runtime/api/app.py` — `CORSMiddleware(allow_origins=["*"])`, and zero auth on any endpoint.

**Risk**: if a container is exposed beyond the trust boundary (LAN, internet, even a shared k8s namespace), anyone can POST `/api/chat` and burn your Anthropic budget.

**Fix paths** (pick one):
- **Shared API key**: `AGENT_API_KEY` env var, FastAPI `Depends(verify_api_key)` checking `X-API-Key` header on every `/api/*` route.
- **JWT / OIDC**: validate tokens issued by your IdP (AAD, Auth0). Heavier setup.
- **Reverse proxy**: nginx + `auth_request`, AAD App Proxy, Cloudflare Access, Tailscale, etc. Move auth out of the runtime entirely.

For internal enterprise tools the API key approach takes 20 minutes and is plenty.

### 10. ADF agent Dockerfile pip-installs deps without a lockfile

**Where**: `agents/adf-agent/Dockerfile`:

```dockerfile
RUN /app/.venv/bin/pip install --no-cache-dir \
        azure-identity azure-mgmt-datafactory mcp python-dotenv
```

Versions float, builds aren't reproducible.

**Fix**: each agent gets a `requirements.txt` (or `pyproject.toml`) committed alongside its Dockerfile. Build step copies it in and `pip install -r`. Better still: per-agent `uv.lock`.

---

## 🟢 Nice to have — contract / DX improvements

### 11. `info` endpoint leaks internal paths

`GET /api/info` currently returns `workspace`, `settings_dir`, `skills_dir` — container-internal paths the frontend has zero use for, and a small information leak.

**Reshape**: return only `{agent_name, model, mcp_tools, hitl_tools, hitl_timeout}`.

**Plus**: add `AGENT_NAME` env var (declared empty in `agents/base.Dockerfile`, set per-agent: `ENV AGENT_NAME=adf` in `agents/adf-agent/Dockerfile`). The frontend's agent picker can then call `GET /api/info` on each container and let the container self-identify, instead of the frontend hard-coding names.

### 12. Document the SSE event contract

`agent_runtime/api/schemas.py` is the source of truth, but a frontend dev shouldn't have to read Python dataclasses to know what to expect.

**Write `doc/sse-contract.md`** covering:
- Each event type and its JSON shape (just dump the dataclass fields)
- Event ordering guarantees:
  - `text_delta*` is always followed by `text_stop`
  - `thinking_start → thinking_delta* → thinking_stop` is a contiguous block
  - Every `tool_call` is followed by exactly one `tool_result`
  - `confirm_request` may interleave between `tool_call` and `tool_result`
  - `done` (with `stop_reason`) terminates the stream
  - `done` with `stop_reason=hitl_timeout` means the round was aborted; the LLM never saw a follow-up turn
  - `token_usage` may arrive before `done`
  - `error` may interrupt the stream at any point
- How to reconstruct an "assistant turn" from the deltas for storage in Postgres `conversation` table
- How to handle `confirm_request`: render UI, POST to `/api/confirm/{request_id}` with `{allowed: true/false}` within `hitl_timeout` seconds (read from `/api/info`)

### 13. SSE keep-alive vs reverse proxies

`sse-starlette` defaults to a `: ping` every ~15s. Anything in front of the runtime needs `proxy_read_timeout` ≥ 30s:
- nginx: `proxy_read_timeout 300s; proxy_buffering off;`
- AWS ALB: idle timeout default 60s, ok
- Cloudflare: 100s by default, ok

Document this in deployment notes.

### 14. MCP shutdown blocks up to 5s

`core/mcp_client.py:194` does `future.result(timeout=5)` during shutdown. If an MCP server hangs, container `SIGTERM` waits 5s before continuing. With many MCP servers, container shutdown gets slow.

**Fix**: lower the timeout to 1s, or make it configurable.

### 15. `tracking.py` price table is hard-coded

`core/tracking.py:7-11` lists per-million-token rates inline. Anthropic price changes silently break cost reporting. Low priority — flag and move on.

### 16. `agent_loop` mutates the caller's `messages` list in-place

In stateless mode, FastAPI parses request body into a fresh list, hands it to `chat_stream`, which hands it to `agent_loop`, which appends assistant + tool_result entries. After the SSE stream completes, that mutated list is discarded — the frontend reconstructs history from SSE events, not from a return value.

This is correct but **not obvious**. Add a one-line docstring note on `agent_loop` that the input list is mutated, and an explicit note in the SSE contract doc that the frontend must build the assistant turn from events, not expect anything back from the chat endpoint beyond the stream.

### 17. `build_system_prompt` runs once at engine init

If skills/MCP change, you need a container restart to pick them up. This is the right behavior for immutable containers — but document it so nobody tries to hot-mount skills and is surprised it doesn't take effect.

---

## Suggested order of operations

If a real frontend is about to start integrating, do this batch first (≤1 hr total):

1. **#1 + #2 + #3 + #4** — fix the per-chat pollution bugs. Frontend would otherwise display incorrect token totals, see other users' todos, etc.
2. **#7** — `git rm -r agent_frontend/` and drop from `pyproject.toml` workspace members. One less broken thing.
3. **#8** — same call: fix or `git rm` the integration test.
4. **#11** — add `AGENT_NAME`, slim `/api/info`. Frontend picker depends on this.
5. **#12** — write `doc/sse-contract.md`. Frontend devs need this as input.

Defer #5, #6, #9, #10, #13–#17 until either (a) production deployment, or (b) the next time you're already in that file for some other reason.
