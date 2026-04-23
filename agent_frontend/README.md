# agent-frontend

Single-page web UI for `agent-runtime`. Python + vanilla JS, no build step.

```
browser ──HTTP──► agent_frontend (this, :8080) ──HTTP+SSE──► agent-runtime (:8001)
                        │
                   storage backend
                   (SQLite or Postgres)
```

The frontend owns **chat history** (sessions, messages); the runtime is
stateless. Storage backend is swappable via one env var.

---

## Run

### The easy way: Docker Compose via `./local.sh`

From the repo root:

```bash
./local.sh up            # build base, start frontend + every agent in agents/
./local.sh up -d         # same, detached
./local.sh down          # stop everything
```

`local.sh` auto-discovers agents, builds the shared base image, launches
the frontend on `:8080`, and exposes each agent on its own host port
(`adf-agent` on `:8001`, etc.). SQLite chat history lives at
`./.data/chat.db` on the host (bind-mounted into the container at
`/app/data/chat.db`).

### Running the web UI directly (no Docker) — for frontend development

When you're iterating on this frontend (HTML/JS/Python), skip the Docker
rebuild loop and run it on the host. Keep the agents dockerized via
`./local.sh up -d`, then:

**1. Start an agent-runtime** (typically via `./local.sh up -d`, listening on `:8001`).

**2. Install frontend deps** (from repo root):

```bash
uv sync --extra frontend                            # SQLite backend
uv sync --extra frontend --extra frontend-postgres  # Postgres backend
```

**3. Run the web UI** (from repo root so `.env` is picked up):

```bash
uv run agent-web                     # localhost:8080
uv run agent-web --port 3000 --host 0.0.0.0
uv run agent-web --reload            # dev auto-reload
```

Open http://localhost:8080.

---

## Environment

`run.py` auto-loads `.env` by walking upward from the current directory —
keep one `.env` at repo root shared with the runtime.

| Var | Default | Meaning |
|---|---|---|
| `AGENT_RUNTIMES` | `http://localhost:8001` | Comma-sep list of runtime base URLs (populates the picker) |
| `CHAT_STORAGE` | `local` | `local` (SQLite) or `postgres` |
| `CHAT_SQLITE_PATH` | `./agent_frontend.db` (local) / `/app/data/chat.db` (Docker) | SQLite file path |
| `CHAT_POSTGRES_URL` | — | `postgresql://user:pass@host:5432/db` (required when `CHAT_STORAGE=postgres`) |
| `CHAT_USER_ID` | `local` | Tenant partition key (single-user for now) |
| `LOG_LEVEL` | `INFO` | |

Switch backends:

```bash
# local sqlite (default) — just run
uv run agent-web

# postgres
echo 'CHAT_STORAGE=postgres' >> .env
echo 'CHAT_POSTGRES_URL=postgresql://user:pass@host:5432/chatdb' >> .env
uv run agent-web
```

The Postgres schema auto-creates on startup (single `sessions` table).

---

## Layout

```
agent_frontend/              ← sibling of agent_runtime/, same wheel
├── __init__.py
├── server.py                ← FastAPI app
├── run.py                   ← uvicorn launcher + .env loader
├── storage/
│   ├── base.py              ← ChatHistoryBackend ABC
│   ├── local.py             ← SQLiteBackend (aiosqlite)
│   ├── postgres.py          ← PostgresBackend (asyncpg)
│   └── manager.py           ← factory: env → backend
└── static/
    ├── index.html
    ├── script.js            ← SSE client, multi-turn reducer, HITL
    └── styles.css
```

No separate `pyproject.toml` — the repo-root one ships both packages. The
frontend-specific deps live under `[project.optional-dependencies].frontend`
to keep minimal runtime builds lean.

---

## API (browser ⇄ this server)

Sessions are stored full-document; the browser PUTs the whole session on
every mutation.

```
GET    /api/agents                              # probe configured runtimes
GET    /api/sessions                            # metadata list
POST   /api/sessions                            # create (body: agent_url, agent_name)
GET    /api/sessions/{id}                       # full session with messages
PUT    /api/sessions/{id}                       # upsert
DELETE /api/sessions/{id}

POST   /api/sessions/{id}/chat                  # proxy → runtime /api/chat (SSE)
POST   /api/sessions/{id}/confirm/{request_id}  # proxy → runtime /api/confirm/{id}

GET    /api/tools  /api/skills  /api/skills/{name}   # meta proxy to first runtime
GET    /api/healthz
```

The runtime protocol (SSE events, HITL semantics) is documented in
`../doc/design.md` §8–§9.

---

## Swapping backends

Subclass `ChatHistoryBackend` in `storage/base.py` and wire it into
`storage/manager.py`'s `create_backend()`. The server only talks to the
interface.
