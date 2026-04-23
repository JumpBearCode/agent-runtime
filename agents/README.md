# Agents

Each subdirectory is one agent. An agent = the shared `agent-runtime` code (in `../agent_runtime/`) + a specific bundle of:

- `skills/`   — `SKILL.md` files describing specialized procedures
- `settings/` — `mcp.json` (MCP servers to launch) + `HITL.json` (tools that require approval)
- `prompts/`  — `system.md` (the agent's identity / operating principles)
- `mcp/`      — optional: source for an MCP server bundled with this agent

## Build

The base image ships the runtime + the FastAPI HTTP layer, no agent identity:

```bash
docker build -f agents/base.Dockerfile -t agent-runtime-base:0.1 .
```

Each agent image extends the base and `COPY`s its bundle in:

```bash
docker build -f agents/adf-agent/Dockerfile -t agent-runtime-adf:0.1 .
```

## Run

```bash
docker run --rm -p 8001:8000 \
    -e MODEL_ID=claude-sonnet-4-6 \
    -e ANTHROPIC_API_KEY=... \
    -v "$(pwd)/agents/adf-agent/workspace:/workspace" \
    agent-runtime-adf:0.1
```

### Container layout

The image splits concerns between two top-level directories:

- `/app` — baked at build time, immutable at runtime. Holds the runtime
  code, its venv, and the agent's `skills/`, `settings/`, `prompts/`,
  `mcp/`.
- `/workspace` — mutable working directory for the agent. Mount a host
  directory here (e.g. `agents/<name>/workspace/`) so anything the agent
  writes — scratch files, new Python projects, generated artifacts — is
  visible locally and persists across container restarts.

All file tools (`read_file`, `write_file`, `edit_file`) and `bash` run with
`cwd=/workspace` and reject paths that escape it.

The container exposes:

- `GET  /api/healthz`
- `GET  /api/info`         → workspace, model, MCP servers, HITL tools, etc.
- `GET  /api/tools`        → registered tool names
- `GET  /api/skills`       → `{name: description}`
- `POST /api/sessions`     → `{id}`
- `GET  /api/sessions`
- `GET  /api/sessions/{sid}`
- `DELETE /api/sessions/{sid}`
- `POST /api/sessions/{sid}/chat`              (SSE stream, body: `{message}`)
- `POST /api/sessions/{sid}/skill/{skill}`     (SSE stream)
- `POST /api/confirm/{request_id}`             (body: `{allowed: bool}`)

## Adding a new agent

1. Create `agents/<name>/` with `skills/`, `settings/`, `prompts/`, optionally `mcp/`.
2. Create `agents/<name>/workspace/.gitkeep` to reserve the mount point
   (the `agents/*/workspace/*` pattern in `.gitignore` keeps AI-written
   files out of git).
3. Write `agents/<name>/Dockerfile` extending `agent-runtime-base`.
4. Build and run on a different host port with
   `-v "$(pwd)/agents/<name>/workspace:/workspace"`.

The frontend selects an agent by pointing at the right container's URL.
