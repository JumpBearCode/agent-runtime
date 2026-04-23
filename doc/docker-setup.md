# Docker Stack — Containerizing agent-runtime for Local Dev

> This document captures the full design of the Docker/Compose stack on
> the `docker` branch: **why** the pieces look the way they do, **what
> was rejected** along the way, and **the bugs hit during rollout**. It's
> a retrospective, meant to onboard future-self (or a collaborator) so
> they understand the architecture without having to re-derive every
> decision from the diff alone.
>
> **Status**: Plan A (local-dev stack + Azure CLI auth) done.
> Plan B (the multi-provider `auth/` package from `auth-flow.md`) not
> started — see §10.
>
> **Related**:
> - [`design.md`](design.md) — runtime architecture
> - [`auth-flow.md`](auth-flow.md) — final auth design (Plan B scope)
> - [`../agents/README.md`](../agents/README.md) — quick-start operator guide
> - [`../local.sh`](../local.sh) — entry point

---

## 1. Goals

Before the `docker` branch there was nothing Docker-specific in the
repo. To run the system you had to `uv run` the frontend manually, and
the "agent" side was only ever operated through the CLI or ad-hoc
`docker run` invocations. Three problems drove the work on this branch:

1. **Operator friction** — starting the whole stack (frontend + one or
   more agent runtimes) took multiple terminals and manual URL wiring.
2. **Multi-agent scaling** — the project is designed around "one agent
   per data platform" (ADF today, Fabric and Snowflake next). Each added
   agent multiplied the manual `docker run` burden.
3. **AI-writable workspace** — the agent's file tools wrote into
   wherever the runtime happened to be started (often repo root or
   `/app`), mixing AI-generated code with source. No clean isolation or
   persistence boundary.

The end state wanted:

- `./local.sh up` starts the whole stack with one command
- Adding a new agent = drop `agents/<name>/` with a Dockerfile and a
  `compose.yml` fragment. No edits to root compose, no edits to any
  script. Frontend and other agents unaffected.
- Agent writes land in `agents/<name>/workspace/` on host, visible and
  version-controllable (the *mount*, not the contents).
- Local dev Azure auth works without `--env-file AZURE_CLIENT_SECRET`
  hacks — just reuse the user's existing `az login`.

All of those are now in place.

---

## 2. Final architecture

```
┌───────────────────────────────── host (macOS / Linux) ─────────────────────────────────┐
│                                                                                        │
│   ./local.sh up                                                                        │
│       │                                                                                │
│       ├─ docker build agent-runtime-base:0.1      (base.Dockerfile)                    │
│       │     ↳ python + runtime venv + az CLI                                           │
│       │                                                                                │
│       ├─ glob agents/*/compose.yml fragments                                           │
│       ├─ compute AGENT_RUNTIMES="http://adf-agent:8000,…"                              │
│       └─ docker compose up --build                                                     │
│             │                                                                          │
│   ┌─────────┴─────────────────────────────────────────────────────────────────┐        │
│   │  docker-compose network "agent-runtime-net"                               │        │
│   │                                                                           │        │
│   │   ┌───────────────┐      ┌────────────────┐      ┌─────────────────┐     │        │
│   │   │ agent-frontend│ ────▶│   agent-adf    │      │  (future)       │     │        │
│   │   │   :8080       │      │   :8000        │      │  agent-fabric   │     │        │
│   │   │               │      │   (host:8001)  │      │  agent-snowflake│     │        │
│   │   │ Dockerfile:   │      │                │      │  …              │     │        │
│   │   │ agent_frontend│      │ Dockerfile:    │      │                 │     │        │
│   │   │  /Dockerfile  │      │ agents/adf-    │      │                 │     │        │
│   │   └───────┬───────┘      │ agent/*        │      │                 │     │        │
│   │           │              └──────┬─────────┘      └─────────────────┘     │        │
│   │           │ SQLite              │ /workspace                              │        │
│   │           ▼                     ▼                                         │        │
│   │      ./.data/chat.db    ./agents/adf-agent/                               │        │
│   │                           workspace/                                      │        │
│   │                                                                           │        │
│   │      ~/.azure ────────── bind-mounted → /home/agent/.azure               │        │
│   │      (shared by every Azure-family agent)                                  │        │
│   └───────────────────────────────────────────────────────────────────────────┘        │
│                                                                                        │
│   browser → http://localhost:8080 (UI)                                                 │
│   curl    → http://localhost:8001 (ADF agent direct — debugging)                       │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### Key containers

| Container | Image | Host port | Purpose |
|---|---|---|---|
| `agent-frontend` | `agent-frontend:0.1` | 8080 | Web UI + SSE proxy + chat-history SQLite |
| `agent-adf` | `agent-runtime-adf:0.1` | 8001 | ADF-bound agent runtime |
| (future) `agent-fabric` | `agent-runtime-fabric:0.1` | 8002 | Fabric agent |

All agents join the same compose network (`agent-runtime-net`). The
frontend reaches each via its service name on that network:
`http://adf-agent:8000`. Host ports are purely for debugging (`curl
localhost:8001/api/info`); they're not on the critical path.

---

## 3. Filesystem split: `/app` vs `/workspace`

Inside every agent container there are two top-level directories that
serve completely different purposes. Getting this separation right took
some iteration (see §A.1).

```
/ (container root)
├── app/                         ← BAKED AT BUILD, immutable at runtime
│   ├── .venv/                   ← runtime Python deps
│   ├── agent_runtime/           ← FastAPI + tool dispatcher + loop
│   ├── skills/                  ← per-agent SKILL.md (COPY'd in)
│   ├── settings/                ← per-agent mcp.json / HITL.json
│   ├── prompts/system.md        ← per-agent identity prompt
│   └── mcp/                     ← per-agent MCP server source
│
└── workspace/                   ← MUTABLE, volume-mounted from host
    └── (AI writes code / scratch files here)
```

### Rules

- `/app` is a server install. Nothing under it should ever be mutated at
  runtime. The AI user has no write permission there.
- `/workspace` is *the project root*, not *a folder containing projects*.
  The runtime's system prompt (`agent_runtime/core/loop.py`
  `build_system_prompt`) explicitly tells the model:
  > Your workspace is `/workspace` — this IS your project root, not a
  > parent directory. For a new project, `uv init .` / `npm init -y`,
  > not `mkdir my-project && cd my-project`.
  Without that nudge the model compulsively creates `/workspace/<name>/`
  and nests projects, which on the host side ends up as
  `agents/adf-agent/workspace/<name>/…`.
- Path enforcement is in *code*, not just prompt: `tools.py:45-46`
  resolves every path against `config.WORKDIR` with `is_relative_to()`
  and rejects escapes. `bash` calls run with `cwd=WORKDIR`. The prompt
  guidance complements the hard guard.

### Per-session isolation — deferred

Multiple chat sessions share the same `/workspace` today. If two
sessions run concurrent file-editing work they'll step on each other.
Acceptable for now (single-user local dev). When multi-session isolation
matters, the escape hatch is per-session dirs like
`/workspace/<session-id>/` — `config.WORKDIR` becomes per-request rather
than process-global. No architectural blocker, just added scope.

---

## 4. Multi-agent discovery via Compose fragments

Docker Compose is declarative and static: it cannot `for d in agents/*`
at runtime. The requirement "drop a new agent dir and it just joins the
stack" required choosing among three patterns:

| Option | How | Verdict |
|---|---|---|
| A. Every agent's service listed in root `docker-compose.yml` | Edit root each time | Rejected — violates "drop-in" |
| B. Generator script produces `docker-compose.yml` from a template | Python / jinja, regenerate before up | Works but adds a codegen step |
| **C. Fragment + `COMPOSE_FILE` merge** ⭐ | Each agent ships `compose.yml`; script globs fragments and joins them via `COMPOSE_FILE=a:b:c` | **Chosen** |

Option C is what Docker officially supports for composing multiple
files, and the fragments can live anywhere in the tree.

### Pattern

- `docker-compose.yml` (root) declares only the **shared**
  infrastructure: the frontend service and the `agent-runtime-net`
  network. No agent services.
- Each `agents/<name>/compose.yml` declares exactly one service block
  for that agent.
- `local.sh` globs `agents/*/compose.yml`, builds the frontend's
  `AGENT_RUNTIMES` env from the discovered names
  (`http://adf-agent:8000,…`), and invokes:

  ```
  COMPOSE_FILE=docker-compose.yml:agents/adf-agent/compose.yml docker compose up
  ```

Adding a Fabric agent in the future is:

1. `agents/fabric-agent/` with Dockerfile + skills/settings/prompts/mcp
2. `agents/fabric-agent/compose.yml` (copy ADF's, change name + port)
3. `./local.sh up` — picks it up automatically

Zero edits to root compose. Zero edits to `local.sh`. Zero edits to
frontend.

### Path resolution gotcha

When Compose merges multiple `-f` files, **every relative path in every
file is resolved against the *project directory*** (the dir of the first
`-f`), **not against the file that declared it**. This was counter to my
initial assumption and caught in review (see §A.2). Fragments must write
paths relative to the project root, even though they live several dirs
deep:

```yaml
# agents/adf-agent/compose.yml
build:
  context: .                                 # resolves to repo root, not agents/adf-agent/
env_file:
  - .env                                     # repo-root .env
volumes:
  - ./agents/adf-agent/workspace:/workspace  # repo-root-relative
```

`local.sh` enforces this by `cd`-ing to repo root before calling
`docker compose`.

---

## 5. `local.sh` — the single entry point

**Where**: repo root. **Why not `scripts/local.sh`**: the root is where
user-facing entry points live; `scripts/` is for internal utilities. The
name pairs deliberately with a future `infra/` directory (Bicep / cloud
deploy) — **local stack lives at root, cloud stack lives under infra/**.

Subcommand style over separate scripts. `./local.sh up` / `down` / `logs`
/ `ps` / `rebuild` cover the 95% case; anything else is passed through
to `docker compose` verbatim.

### What it does before compose

```bash
# 1. Build the base image — agents FROM it. Compose can't handle
#    FROM-image ordering between services, so we do it separately.
docker build -f agents/base.Dockerfile -t agent-runtime-base:0.1 .

# 2. Discover agent fragments.
AGENT_FRAGMENTS=$(find agents -mindepth 2 -maxdepth 2 -name compose.yml)

# 3. Compute frontend's AGENT_RUNTIMES from service names.
export AGENT_RUNTIMES="http://adf-agent:8000,..."

# 4. Merge root compose with every fragment.
export COMPOSE_FILE="docker-compose.yml:agents/adf-agent/compose.yml:..."

# 5. Hand off.
docker compose up --build
```

### Cleanup (`./local.sh down`)

Thin wrapper over `docker compose down` — stops + removes the compose-
managed containers and network, but **does not remove images**. Images
stay cached so the next `up` is fast.

If you have **stray containers** that were started outside compose
(e.g., earlier `docker run` experiments), compose won't touch them — you
need `docker rm -f <name>`. We hit this during rollout (see §A.4) and
it's worth remembering: only containers with the compose project label
are in scope for `down`.

### A dev-mode override was considered and removed

The original design had `docker-compose.dev.yml` that bind-mounted
`agent_frontend/` into the container plus `--reload`. The idea was
"edit HTML/JS on host, browser reflects changes without rebuild."

Removed because **there's a better dev path already**: run the frontend
on the host with `uv run agent-web --reload`, point it at the Docker-
ized agents. No container round-trip at all.

Mental model after removal:
- `./local.sh up` = prod-like integrated startup (demo, integration test,
  cloud deploy rehearsal)
- `uv run agent-web --reload` = frontend dev iteration (host process
  talking to dockerized agents)

---

## 6. Frontend container

The frontend is a thin FastAPI app (SSE proxy to agents + SQLite chat
history storage). It doesn't need to be containerized for the system to
work — running it via `uv run agent-web` is a perfectly fine dev flow —
but it **is** containerized so `./local.sh up` delivers one-command
integrated startup and so future cloud deploys have a consistent image.

### Dockerfile conventions

- Multi-stage: `builder` (uv sync + venv) → `runtime` (slim Python +
  copied venv). ~200 MB final vs ~700 MB single-stage.
- Entry: `agent-web` (declared in `pyproject.toml` `[project.scripts]`
  as `agent_frontend.run:main`). CMD runs it with `--host 0.0.0.0
  --port 8080`.
- SQLite path `CHAT_SQLITE_PATH=/app/data/chat.db`, with `/app/data`
  bind-mounted to host `./.data/` (see §7).

### Shebang trap (gory detail — see §A.3)

`uv pip install .` bakes the builder-stage interpreter path into entry-
script shebangs. If the builder venv is at `/build/.venv` and the
runtime copies it to `/app/.venv`, the resulting `agent-web` script
starts with `#!/build/.venv/bin/python` — which doesn't exist in the
runtime stage. Container loops on `exec agent-web: no such file or
directory`.

Fix: **build the venv at the final path**. Set `WORKDIR /app` in the
builder stage (not `/build`), and `uv pip install .` writes the correct
shebang. The runtime stage copies the venv to the same `/app/.venv`
path. No shebang rewriting, no entrypoint gymnastics — just matching
paths.

### Persisted state: SQLite

- **Why SQLite at all**: frontend owns chat history. Runtime is
  stateless. Simplest persistence for single-user local dev.
- **Where it lives on host**: `./.data/chat.db` (via bind mount
  `./.data:/app/data`).
- **Previously** the file was at repo root (`./agent_frontend.db`);
  migrated to `./.data/` so future persistent state (e.g., token cache
  per `auth-flow.md` Plan B) has a unified place to land.
- `.gitignore` excludes `.data/` and all `*.db` files.

---

## 7. Local Azure auth (Plan A)

### Problem

The ADF MCP server uses `DefaultAzureCredential()`. In cloud (Azure App
Service / AKS) that resolves via Managed Identity. Locally there's no
MI — the credential chain falls through to `AzureCliCredential`, which
shells out to `az account get-access-token`. **But** the base image
didn't have `az` installed, and even if it did, there was no `~/.azure/`
cache to read from inside the container. Every ADF call failed.

### Resolution

Two mechanical changes, one architectural.

**Architectural**: we adopted the user's domain-shape — ~90% of agents
will be Azure-family (ADF, Fabric, Synapse, …), the rest non-Azure
(Snowflake, ADO). Three options:

| | Approach | Non-Azure agent cost | Clean? |
|---|---|---|---|
| A. | az CLI in `base.Dockerfile` | +500 MB image | Violates "base is minimal" slightly |
| B. | Intermediate `azure-base.Dockerfile` between base and Azure agents | 0 | Cleanest, extra build step |
| C. | Per-Azure-agent install | 0 | DRY violation across 3+ agents |

Chose **A** because user reported Azure-first workload. Re-evaluating to
B is low-cost if a second non-Azure agent joins and the 500 MB becomes
noticeable.

**Mechanical 1**: `base.Dockerfile` installs `azure-cli` via Microsoft's
apt repo. Pinned base to `python:3.12-slim-bookworm` because the apt
repo has no Debian trixie (13) release yet; `python:3.12-slim` defaults
to trixie and breaks the install (see §A.5).

**Mechanical 2**: `agents/adf-agent/compose.yml` bind-mounts
`${HOME}/.azure:/home/agent/.azure` (read-write; see §A.6).

### Data flow

```
host:
  ~/.azure/             ← populated by your `az login`
     azureProfile.json  ← subscription + tenant
     msal_token_cache.* ← refresh + access token material
                        │
                        │ bind-mount
                        ▼
container:
  /home/agent/.azure/   ← AZURE_CONFIG_DIR default for agent user
                        │
                        │ az + AzureCliCredential read/refresh from here
                        ▼
  DefaultAzureCredential()
     .get_token(scope="https://management.azure.com/.default")
                        │
                        ▼
  DataFactoryManagementClient(credential, subscription_id) → ADF API
```

### Cloud path (unchanged)

When the same image runs in Azure App Service with a Managed Identity
assigned, `DefaultAzureCredential` resolves MI *before* ever hitting
`AzureCliCredential`. The `~/.azure` mount is absent (no compose in
cloud), so the chain skips CLI entirely. **Same Dockerfile, same
Python code, works in both environments.** The whole "500 MB az bloat"
is pure dev-image cost and has zero production behavior impact.

### Why this is *not* a stopgap

Earlier framing suggested Plan A was "a temporary hack until Plan B
lands." Re-reading `auth-flow.md` §10 clarified this: **Plan A is the
`service` mode's local-dev implementation** — it's how the cloud's
Managed Identity gets simulated on a laptop. Plan B adds `device_code`
mode *on top* for cross-provider (Snowflake, ADO, Azure-privileged)
scenarios. Plan A isn't replaced when B lands; they coexist.

---

## 8. Other fixes along the way

Two pre-existing bugs surfaced during Docker rollout.

### Pre-existing #1: unconditional postgres import

`agent_frontend/storage/manager.py` did `from .postgres import
PostgresBackend` at module top. `postgres.py` imports `asyncpg`, which
lives in the optional `[frontend-postgres]` extra. The Docker image only
installs `[frontend]` (SQLite is enough). Container crashed on startup
with `ModuleNotFoundError: asyncpg` even though the code never touched
postgres.

Fix: lazy-import `PostgresBackend` inside the `if mode == "postgres":`
branch of `create_backend()`. Clean and minimal — local users never
import asyncpg, postgres users still do.

### Pre-existing #2: base workdir semantics

Base image had `AGENT_WORKDIR=/app`. Since `tools.py` restricts file
I/O to `$AGENT_WORKDIR`, the AI's `write_file` calls landed in `/app`,
next to the runtime code. Latent bug — nobody had exercised file tools
heavily enough to notice.

Fix: separate `/app` (install dir) from `/workspace` (AI working dir)
as described in §3.

---

## 9. What `./local.sh up` runs through, end-to-end

For the curious — the full sequence from command to working browser URL:

```
1. ./local.sh cd's to repo root
2. docker build -f agents/base.Dockerfile → agent-runtime-base:0.1
     - python:3.12-slim-bookworm
     - runtime venv + agent_runtime package
     - az CLI (for AzureCliCredential fallback)
     - user `agent`, /workspace dir, /app at WORKDIR
3. Glob agents/*/compose.yml → one fragment (adf-agent) for now
4. Compute AGENT_RUNTIMES="http://adf-agent:8000"
5. Compose build:
     - agent-runtime-adf:0.1 (FROM base, + ADF deps + identity COPY)
     - agent-frontend:0.1    (from agent_frontend/Dockerfile)
6. Compose up:
     - network agent-runtime-net created
     - agent-adf starts, listens :8000 in-network, :8001 on host
     - agent-frontend starts, listens :8080 on host, knows AGENT_RUNTIMES
7. Healthchecks settle (curl /api/healthz)
8. → http://localhost:8080 in browser
```

---

## 10. Next steps — Plan B and beyond

Plan A ends here. What's *not* done (and explicitly out of scope of
this branch):

| # | Thing | Where it's specified | Size |
|---|---|---|---|
| B1 | `auth/` top-level package | `auth-flow.md` §10.2 | 15+ new files, design is finalized |
| B2 | Device-code provider for Snowflake / ADO / Azure-privileged | `auth-flow.md` §7, §10.3 | Part of B1 |
| B3 | JWT verification on `/api/chat` | `auth-flow.md` §9 | Part of B1 |
| B4 | MCP `ContextualCredential` + thread-local token | `auth-flow.md` §8.3 | Part of B1 |
| B5 | Per-session workspace isolation (§3 above) | Not specced yet | Orthogonal |
| B6 | Refresh-token flow on top of device code | `auth-flow.md` §12.2 (explicitly cut from v1) | Extended MVP — nice UX upgrade |
| B7 | Azure Bicep infra under `infra/` | Not started | Separate effort |

Recommended order: B1 (foundational), B6 (small UX delta, big quality-of-
life), then B7. B5 is independent — pick up when multi-session interference
becomes a real problem.

---

## Appendix A — Bugs encountered during rollout

Kept here because the scars are useful. Every one of these cost 10–30
minutes to diagnose.

### A.1 `AGENT_WORKDIR=/app` in base

Original base.Dockerfile set `AGENT_WORKDIR=/app`, conflating the
install directory with the agent's working directory. AI's file tools
wrote next to runtime code. See §3 for fix.

### A.2 Compose fragment path resolution

First version of `agents/adf-agent/compose.yml` had
`build: { context: ../.., dockerfile: agents/adf-agent/Dockerfile }`.
Intuition: relative to fragment file. **Reality** when merged via
`COMPOSE_FILE`: all paths relative to *project directory* (first `-f`
file's dir). Context resolved to `/Users/wqeq/Desktop` (one dir above
repo root), build failed with "no such file".

Fix: paths in fragments are written relative to repo root (since
`local.sh` always invokes from there). See §4.

### A.3 Frontend `agent-web` shebang

Covered in §6. `uv pip install .` writes the builder-stage python path
into entry-script shebangs. Multi-stage COPY to a different final path
breaks them. Aligning builder and runtime venv paths is the fix.

### A.4 Stray containers from before compose existed

During rollout, a 30-hour-old `adf-agent` container (started manually
via `docker run` before compose was set up) was occupying port 8001.
`./local.sh up` failed with "port in use" but didn't identify the
culprit helpfully.

`docker compose down` can't touch containers it didn't create.
`docker rm -f adf-agent` was needed. No code change — a diagnostic
reminder.

### A.5 Debian trixie vs Microsoft apt repo

`python:3.12-slim` has tracked Debian trixie (13) since its late-2025
rebase. Microsoft's Azure CLI apt repository doesn't publish for trixie
yet. `apt-get install azure-cli` → `404 Not Found`.

Fix: pin `python:3.12-slim-bookworm`. Long-term: switch back to
unpinned when Microsoft publishes trixie. Worth leaving a TODO on the
pin.

### A.6 `~/.azure` read-only mount

First attempt mounted `${HOME}/.azure:...:ro`. az CLI wants to create
`extensionIndex.json` at startup — read-only filesystem → `OSError:
[Errno 30] Read-only file system`. Every `az` call crashed before
doing anything.

Read-write mount accepted for local dev (container writes token
refreshes back to host under the same user identity; blast radius
effectively zero). Cleaner alternative (entrypoint that copies
`~/.azure` to a writable container-local path and sets
`AZURE_CONFIG_DIR`) deferred until proven necessary.

### A.7 `docker-compose.dev.yml` was overengineered

Added then removed within one session. See §5. The frontend has a
perfectly good host-side dev path (`uv run agent-web --reload`), and
adding a container-side hot-reload overlay just multiplied the mental
model for no user benefit. Deletion reduced surface area; the lesson
is "don't containerize dev paths that already work fine off-container".
