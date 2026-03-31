# Ralph Wiggum Loop Prompts — Agent Frontend

## Architecture: Plan B (Minimal Callback Hook)

agent_runtime 只改 loop.py（加 ~30 行 on_event callback），其余全部冻结。
agent_frontend 作为独立 uv workspace 包，通过 engine.py 的 queue bridge 桥接 sync→async。

```
agent_runtime/          ← 冻结，仅 loop.py 加 on_event 参数
  loop.py               ← +30 lines (backward compatible)
  __main__.py           ← 保留不动

agent_frontend/         ← 新 uv workspace 包
  engine.py             ← async queue bridge: sync agent_loop → async event stream
  schemas.py            ← EngineEvent dataclasses + to_sse()
  cli/                  ← Rich CLI (prompt-toolkit + Rich Live)
  web/                  ← FastAPI + SSE + chatui-sso 风格 UI
```

## Prompts

| # | File | Focus | Deps | Max Iterations |
|---|------|-------|------|----------------|
| 1 | [prompt-1-foundation.md](prompt-1-foundation.md) | uv workspace + loop.py callback + engine.py | None | 15 |
| 2 | [prompt-2-rich-cli.md](prompt-2-rich-cli.md) | Rich CLI frontend | Prompt 1 | 20 |
| 3 | [prompt-3-web-frontend.md](prompt-3-web-frontend.md) | Web frontend (FastAPI + SSE) | Prompt 1 | 25 |
| 4 | [prompt-4-adf-integration.md](prompt-4-adf-integration.md) | ADF MCP + Skills | Prompt 1+2+3 | 10 |

## Execution Order

1. **Prompt 1** first (foundation, everything depends on it)
2. **Prompt 2** next (CLI is lighter, faster to verify engine works)
3. **Prompt 3** after (Web is heavier, benefits from engine being proven)
4. **Prompt 4** last (integration, needs both frontends)

Prompt 2 and 3 can theoretically run in parallel (both only depend on Prompt 1),
but running 2 first helps catch engine bugs faster.

## Key Constraints

- agent_runtime: ONLY loop.py modified (~30 lines), all other files frozen
- Package management: uv workspace only, no pip
- No OAuth, no Postgres, no Redis — local JSONL sessions
- Web frontend: plain HTML/CSS/JS, no React/Vue/Angular
- CDN only for marked.js + highlight.js
