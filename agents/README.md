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

### Authenticating to downstream services

Most agents here talk to Azure (ADF, Fabric, Synapse, …), which use
`DefaultAzureCredential` — it walks a chain until something works:

1. Environment variables (`AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / …)
2. Workload Identity / Managed Identity (cloud deploy)
3. **Azure CLI** — reads `~/.azure/` for a cached `az login` session

The base image installs `az` so step 3 works out of the box. Each Azure
agent then bind-mounts the host's `~/.azure` into the container at
`/home/agent/.azure`, so the container picks up your existing login:

```yaml
# in agents/<name>/compose.yml
volumes:
  - ${HOME}/.azure:/home/agent/.azure
```

Cloud deploy uses Managed Identity (step 2), skipping the mount — same
Dockerfile works in both environments. Non-Azure agents (Snowflake, ADO)
don't use this path and will ship their own auth provider once the
`auth/` package from `doc/auth-flow.md` is implemented.

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

The repo-root `./local.sh` auto-discovers any agent with a `compose.yml`
fragment, so adding one is drop-in — no edits to root compose, no edits
to the script.

1. Create `agents/<name>/` with `skills/`, `settings/`, `prompts/`, optionally `mcp/`.
2. Create `agents/<name>/workspace/.gitkeep` to reserve the mount point
   (`.gitignore` already excludes workspace contents).
3. Write `agents/<name>/Dockerfile` extending `agent-runtime-base`.
4. Write `agents/<name>/compose.yml` — a single-service fragment.
   Copy `agents/adf-agent/compose.yml` and change:
   - service name (`adf-agent` → `<name>`)
   - `container_name`
   - `image` tag
   - build `dockerfile` path
   - `AGENT_NAME` env
   - host port (`8001` → next free: `8002`, `8003`, ...)
   - workspace mount path
   - Keep the `${HOME}/.azure` mount if the agent talks to Azure;
     drop it and add an auth-provider mount when the target isn't Azure.
5. Run `./local.sh up` from the repo root.

The frontend receives `AGENT_RUNTIMES=http://<each-name>:8000,...` at
launch and lets the user pick between them.
