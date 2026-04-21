# Chat history storage — design & rationale

Written 2026-04-21, after the V1 web UI was rebuilt to talk to the
stateless runtime. This doc captures **what's in the DB, why the schema
looks the way it does, and when to change it**.

- **Location**: `agent_frontend/storage/`
- **Backends**: `local.py` (SQLite via aiosqlite) and `postgres.py` (asyncpg)
- **Selected by env var**: `CHAT_STORAGE=local` (default) or `postgres`

---

## 1. Scope — what this DB is for

> **This DB exists so the UI can reopen old chats.** Nothing else.

Concretely: when the user clicks a session in the sidebar, the browser
needs to rebuild the chat view and — more importantly — be able to POST
the full `messages[]` history back to `agent_runtime` on the next turn
(the runtime is stateless; every request carries full history, see
`design.md §3`).

It is **not** the system of record for:
- LLM/tool observability (→ **LangSmith**, instrumented inside
  `agent_runtime`)
- Token-level billing / usage analytics (→ LangSmith)
- Per-tool latency metrics (→ LangSmith)
- Message-level evaluation (thumbs up/down, comments) —  not yet in scope;
  if/when needed, LangSmith's feedback API is the first choice

Drawing the boundary tightly matters because it justifies the schema
choice below.

---

## 2. Schema — single table, JSONB messages

```sql
CREATE TABLE sessions (
    id          TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT 'local',
    agent_name  TEXT NOT NULL,
    agent_url   TEXT NOT NULL,
    title       TEXT,
    messages    JSONB NOT NULL,      -- TEXT (JSON) in SQLite
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, id)
);
CREATE INDEX idx_sessions_user_updated ON sessions (user_id, updated_at DESC);
```

`messages` is the **entire conversation** serialized as a JSON array in
Anthropic block format, e.g.:

```json
[
  {"role":"user", "content":"list all linked service"},
  {"role":"assistant", "content":[
     {"type":"text","text":"I'll fetch all linked services..."},
     {"type":"tool_use","id":"toolu_01...","name":"mcp_adf_list_linked_services","input":{}}
  ]},
  {"role":"user", "content":[
     {"type":"tool_result","tool_use_id":"toolu_01...","content":"..."}
  ]},
  {"role":"assistant", "content":[
     {"type":"text","text":"Here are the 2 Linked Services..."}
  ]}
]
```

This is **byte-identical to what gets POSTed to `agent_runtime`'s
`/api/chat`** (after stripping UI-only fields — see §5). No format
translation on read.

`user_id` is a partition key reserved for future multi-tenancy. Today
it's hard-coded to `"local"` via `CHAT_USER_ID` env var.

---

## 3. Why one table, not two or three

We considered three shapes. The reference implementation
(`generic-ai/app/infrastructure/postgresql.py`) uses shape **B**.

| Shape | Structure | Pros | Cons |
|---|---|---|---|
| **A (what we ship)** | `sessions` only; `messages` is a JSON column | Minimal code. Schema ≡ Anthropic wire format — zero translation. One query rebuilds the whole thread. | Every turn rewrites the full row. No SQL at message granularity. No per-message edits. |
| **B (`generic-ai` style)** | `sessions` + `messages` table (one row per message, `sequence_number`, `is_satisfy`, `comment`) | Incremental append. SQL over individual messages (feedback, stats). | 2× DB round trips for common reads (JOIN + reorder). Still stores block arrays as JSON inside the `content` column — assistant messages with multiple blocks aren't further decomposed. |
| **C (3-table)** | B + `tool_calls` table keyed on `message_id + block_index` | Pure-SQL tool-usage analytics. | Anthropic `tool_use` is a content block *inside* an assistant message; three-way atomic writes per turn; read path reassembles the Anthropic shape from three tables. Rarely worth it. |

### Why A over B

Message-granularity queries are *exactly* what LangSmith covers
(per-run traces, token usage, tool invocations, latencies, feedback
API). Since LangSmith is a company-wide mandate for agent telemetry,
shape B's main feature — per-message SQL — is already provided by a
better system. B adds code in this project without buying capability
not already available elsewhere.

### Why not C

Even `generic-ai` doesn't split tool_use into its own table. Tool usage
analysis belongs in LangSmith; building a third table to duplicate what
LangSmith indexes natively is pure duplication.

### What we lose by picking A

Four things, listed by likelihood of hurting:

1. **Concurrent writes are last-write-wins.** Two tabs editing the same
   session → the later PUT overwrites the earlier. Not a problem for
   single-user-single-tab; becomes real with multi-device sync.
2. **No per-message operations.** Can't delete / edit / regenerate a
   single turn without downloading + mutating the whole JSON array.
3. **Row grows with history length.** See §4.
4. **Admin / support can't query "what did user X say in session Y
   message 5" in SQL** — they parse JSON. Minor.

None of these are deal-breakers at our scale (tens to hundreds of
users).

---

## 4. Scaling characteristics

Write cost per turn, as `messages` grows:

| History size | JSON bytes | Postgres UPSERT | SQLite UPSERT |
|---|---|---|---|
| 10 turns | ~20 KB | <5 ms | <2 ms |
| 50 turns | ~100 KB | ~10 ms | ~5 ms |
| 200 turns, tool-heavy | ~500 KB | ~20 ms | ~10 ms |
| 500 turns, extreme | 2–5 MB | ~100 ms | ~50 ms |

Postgres auto-TOASTs fields above 2 KB (compressed + out-of-line
storage), so growth isn't linear past the TOAST threshold.

Read: one `SELECT` + JSON parse. 500 KB parses in <10 ms in the
browser.

### The real session-length ceiling is upstream

Anthropic's context window is ~200K input tokens. A session that
approaches that will hit the model's limit before it hits any DB
limit. History management (summarization, sliding window, or "start a
new chat") is a runtime-level concern, not a storage concern.

---

## 5. What's **not** in the DB

The browser stores these in `messages[]` but they're **stripped** by
`agent_frontend/server.py::_strip_ui_fields` before the payload reaches
the runtime:

- `meta` on assistant messages (token usage, stop reason)
- `args_summary` inside `tool_use` blocks (short label for the UI's
  collapsed tool card)

Reason: Anthropic's API rejects unknown keys on `tool_use` blocks with
a `400 invalid_request_error`. The stripping is a belt-and-suspenders
guard: any future UI-only field we add to a block will be silently
sanitized, not blow up the second turn.

These are kept in the DB (not stripped on save) because the UI needs
them on session reload to re-render token bars and tool card summaries
without re-running the model.

---

## 6. When to upgrade to shape B

Trigger: any of the following becomes a real requirement.

- **Per-message user actions**: delete one turn, edit & resend, regenerate
  just the last assistant response.
- **Per-message user feedback stored locally** (👍 / 👎 / comments) that we
  don't want to round-trip through LangSmith.
- **Multi-tab or multi-device editing** of the same session.
- **Admin tooling** querying "which user asked X in which session".

The migration is contained to `agent_frontend/storage/`:

1. New schema: split `sessions.messages` into a `messages` table with
   `(session_id, sequence, role, content JSONB, created_at, ...)`.
2. `ChatHistoryBackend` interface doesn't change — `get_session()` still
   returns `{id, ..., messages: [...]}`. The backend rebuilds the array
   from rows internally.
3. One-shot migration script: for each existing `sessions` row, iterate
   its JSON and INSERT messages rows.

No changes to `server.py`, `script.js`, or `agent_runtime`.

---

## 7. Relationship to LangSmith

**LangSmith = observability.** **This DB = session persistence for the UI.**
They are orthogonal and should stay that way.

| Concern | Owner |
|---|---|
| Resume yesterday's chat | This DB |
| "What messages did I send in session X?" | This DB |
| Sidebar list of sessions | This DB |
| Chat title, agent binding | This DB |
| LLM-call traces, token usage, latency | LangSmith |
| Tool-invocation analytics | LangSmith |
| Per-run feedback & evals | LangSmith |
| Reproducing a failed run for debugging | LangSmith |

LangSmith instrumentation belongs inside `agent_runtime`
(`core/loop.py` around `_stream_response`, where the Anthropic SDK
call happens), not in the frontend. The `trace_id` already carried in
`POST /api/chat` can double as the LangSmith run parent-id.

Skipping this DB in favor of LangSmith alone would mean: every chat
list / session open → cross-internet call → LangSmith rate limits, SaaS
outages directly break the UI, schema mismatch (LangSmith indexes by
run-id tree, not linear user conversation). Not viable.

---

## 8. Config summary

```
CHAT_STORAGE         local | postgres            (default: local)
CHAT_SQLITE_PATH     ./agent_frontend.db        (default)
CHAT_POSTGRES_URL    postgresql://user:pass@host:5432/db
CHAT_USER_ID         local                      (default; placeholder until auth)
```

The schema is auto-created on first startup for both backends
(`CREATE TABLE IF NOT EXISTS` / `IF NOT EXISTS` indexes).
