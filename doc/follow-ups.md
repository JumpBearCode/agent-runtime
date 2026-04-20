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

---

## ✅ Resolved

- **#1, #2** — per-thread Todo (`tools_mod.set_thread_todo`) + per-chat `TokenTracker` constructed inside `chat_stream`. Token totals and todos are now isolated per request.
- **#3, #4** — `core/compression.py` deleted entirely; `auto_compact` / `micro_compact` / `should_compact` / transcript writes / `compact` tool / `_inject_todo` all gone. Will revisit compaction later with a cleaner design.
- **#5** — `core/loop.py` now uses `logging.getLogger(__name__)`; all `print()` + `sys.stdout.write()` + ANSI escapes removed. `api/app.py` configures structured `%(asctime)s %(levelname)s %(name)s :: %(message)s` via `logging.basicConfig(force=True)`. `LOG_LEVEL` env var (default `INFO`).
- **#8** — broken `tests/test_integration.py` deleted; replaced with full pytest suite (`tests/test_*.py`, 91 tests, runs in <1s). `[project.optional-dependencies] test` adds pytest + httpx; `uv run --extra test pytest`.
- **#10** — `agents/adf-agent/pyproject.toml` + `uv.lock`; Dockerfile installs via `uv export → pip install -r requirements.txt`. Builds reproducible.
- **#11** — `/api/info` returns only `{agent_name, model, mcp_tools, hitl_tools, hitl_timeout}`. `AGENT_NAME` env var declared empty in `base.Dockerfile`, set per-agent in each `Dockerfile`.
- **#18** — `self._on_event` cross-chat race; resolved by passing `on_event` directly into `_RegistryConfirmHook` constructor (the hook is already per-trace).
- **(bonus)** — While writing tests, found `_ConfirmRegistry.cancel_trace` was setting `slot.result = False`, but the hook treats `False` as "user explicitly denied → continue round" and `None` as "cancelled → AbortRound". Cancel now leaves `result = None` so SSE-disconnect properly aborts the round.

---

## 🔴 Round 2 — additional findings from engine.py review (2026-04-20)

### 18. `self._on_event` cross-chat overwrite ✅ FIXED

**Where**: `engine.py:chat_stream` did `self._on_event = on_event`. The HITL confirm hook then read `self._engine._on_event` to emit `confirm_request` events.

**Symptom**: two concurrent chats — chat A and chat B — both run `self._on_event = X`. Whichever started later wins. When chat A's tool triggers HITL, the hook reads the engine attribute → finds chat B's callback → pushes `confirm_request` to chat B's SSE stream. Chat A's frontend never sees the prompt; chat B's frontend gets a phantom prompt for a tool it never invoked. Whoever resolves it (or the timeout fires) routes back to the wrong agent thread.

**Fix**: stop storing `on_event` on the engine. The `_RegistryConfirmHook` already gets a fresh instance per `chat_stream` call — pass `on_event` directly into its constructor. Engine no longer holds any per-chat state.

### 19. ThreadPoolExecutor non-daemon threads → shutdown can hang up to HITL_TIMEOUT

**Where**: `engine.py:193-196` creates a stdlib `ThreadPoolExecutor`. `engine.py:324` does `self._executor.shutdown(wait=False)` in the lifespan exit.

**Why it hangs**: stdlib `ThreadPoolExecutor` uses non-daemon worker threads. `concurrent.futures.thread._python_exit` is registered as `atexit` and joins all worker threads before the interpreter is allowed to exit. `shutdown(wait=False)` returns immediately, but the atexit join blocks until each worker returns. If even one chat thread is sitting in `slot.event.wait(600)` (HITL prompt mid-flight when SIGTERM arrives), the process can't terminate until that wait expires — which may exceed the container grace period and trigger SIGKILL anyway, leaking in-flight work and SSE connections.

**Fix**: in `shutdown()`, drain pending HITL slots first so blocked threads wake up and unwind:

```python
def shutdown(self):
    self._confirm_registry.cancel_all()        # new method — sets every slot's event
    self.mcp.shutdown()
    self._executor.shutdown(wait=True, cancel_futures=True)
```

Then update `_ConfirmRegistry`:

```python
def cancel_all(self):
    with self._lock:
        slots = list(self._slots.values())
        self._slots.clear()
        self._by_trace.clear()
    for slot in slots:
        if not slot.event.is_set():
            slot.result = False
            slot.event.set()
```

Also add a tighter overall deadline in `lifespan`'s exit so a stuck MCP server can't indefinitely block the join.

### 20. `asyncio.get_event_loop()` is deprecated inside async functions

**Where**: `engine.py:277` — `loop = asyncio.get_event_loop()` inside `chat_stream` (an `async def`).

**Why it matters**: in Python 3.12+ this emits `DeprecationWarning`; in 3.14+ it may behave differently / raise. Inside an async function the modern idiom is `asyncio.get_running_loop()`, which is faster and unambiguous (it returns the loop the coroutine is actually running on, never creates a new one).

**Fix**: trivial one-line swap. No semantic change today; future-proofs the upgrade path.

### 21. SSE queue overflow silently drops events

**Where**: `engine.py:278` — `asyncio.Queue(maxsize=1024)`. `on_event` (called from the agent thread) does `loop.call_soon_threadsafe(queue.put_nowait, evt)`.

**Symptom**: if the frontend / network can't keep up with the agent's event production rate (text_deltas are emitted per token, easily 50/sec), the queue can fill. When the scheduled `put_nowait` runs on the event loop and finds the queue full, it raises `asyncio.QueueFull`. asyncio logs this as an "exception in callback" but the agent thread keeps running and never knows. Result: silently lost text/tool events, and the frontend's reconstructed conversation is incomplete.

**Fix options** (pick one):
- **Detect and log**: catch `QueueFull` inside `on_event`'s scheduled callable and `logger.warning`. At least operators can see backpressure.
- **Block the producer**: replace `call_soon_threadsafe(queue.put_nowait, evt)` with `asyncio.run_coroutine_threadsafe(queue.put(evt), loop)` so the agent thread blocks when the queue is full. Trade-off: the agent thread will now stall on slow consumers, which may interact badly with HITL timeouts.
- **Increase headroom**: bump maxsize to 8192. Cheap, doesn't fix the underlying contract issue.

For internal-tool scale, "detect and log" + an oversize bump is enough. Consider properly when load testing.
