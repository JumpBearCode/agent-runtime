# Stress / load tests

**These files are NOT collected by pytest.** The filenames do not start with
`test_` / do not end in `_test.py`, so `pytest` skips them by default
collection rules (and `pyproject.toml` uses the default pattern).

Everything in here requires a live stack:

- `agent_frontend` running on `:8080`
- `agent-runtime` container running on `:8001` (or whatever the frontend is pointed at)
- A real Anthropic API key — these scripts make real LLM calls and cost real tokens

## Scripts

### `stress_chat_concurrent.py`

Drives 10 sessions in parallel through the frontend. Validates:

- SSE event ordering (`tool_call` → `confirm_request` → `tool_result`, `done` last)
- 1:1 pairing of `tool_call.id` with `tool_result.id`
- HITL `request_id` uniqueness across sessions (the race design.md §8 warns about)
- Every `/api/confirm` returns 200
- No `hitl_timeout` / `error` stop_reasons

Exit code 0 on clean run, 1 if any cross-check fires.

**Run:**

```bash
# 1. start runtime + frontend separately (see repo-root README)
# 2. from repo root:
uv run python tests/stress/stress_chat_concurrent.py
```

Expect ~20–40 s wall clock. Session IDs created during the run are deleted at
exit so nothing leaks into `agent_frontend.db`.

**Tuned for the ADF agent.** If you point the frontend at a different agent,
edit `USER_PROMPTS` — the bash-forcing prompt in particular is fragile (see
`doc/stress-test-observations.md` §3).

## When to run

- Before shipping changes to `agent_runtime/api/` (chat, confirm, SSE)
- Before shipping changes to `agent_frontend/server.py` (proxy, streaming)
- After touching HITL routing (`core/hooks.py`, confirm registry)
- As a sanity check after bumping httpx / fastapi / uvicorn

## Adding more stress scripts

Name the file anything **except** `test_*.py` or `*_test.py` so pytest keeps
ignoring it. `stress_*.py` is a good prefix.
