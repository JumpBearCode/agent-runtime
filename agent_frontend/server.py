"""FastAPI app — serves the UI and brokers between browser and agent-runtime.

Three responsibilities:

1. Serve static files (index.html + script.js + styles.css).
2. Session CRUD → storage backend (local SQLite or Postgres).
3. Proxy streaming chat + HITL confirm to one of the configured agent
   runtimes. The runtime is stateless; sessions live here.

A session is the unit of persistence. The browser builds Anthropic-shape
`messages[]` from SSE events and PUTs the full session back on every
completed round. We don't parse SSE on the server — we just forward bytes.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from auth import UserIdentity, parse_easyauth_headers

from .storage import ChatHistoryBackend, close_backend, get_backend

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "static"

# Comma-separated list of runtime base URLs. First one is the default for
# new sessions; all are probed for the agent picker.
_AGENT_RUNTIMES: List[str] = [
    u.strip() for u in os.getenv("AGENT_RUNTIMES", "http://localhost:8001").split(",") if u.strip()
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Auth dependency ───────────────────────────────────────────────────────
# EasyAuth sits in front of this app and injects X-MS-CLIENT-PRINCIPAL-*
# plus X-MS-TOKEN-AAD-ID-TOKEN on every authenticated request. The
# dependency extracts a UserIdentity; downstream routes use user.user_id
# for session ownership and forward user.raw_token as Bearer to the
# runtime (which does the actual JWT signature check).
#
# AUTH_DEV_MODE=1 bypasses header parsing and synthesizes a dev user.


async def require_user(request: Request) -> UserIdentity:
    user = parse_easyauth_headers(request.headers)
    if user is None:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return user


# ── Lifespan ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.storage = await get_backend()
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
    logger.info("agent_frontend ready: runtimes=%s", _AGENT_RUNTIMES)
    try:
        yield
    finally:
        await app.state.http.aclose()
        await close_backend()


app = FastAPI(title="Agent Frontend", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ── Index ─────────────────────────────────────────────────────────────────


@app.get("/")
async def index():
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/api/healthz")
async def healthz():
    return {"status": "ok"}


# ── Agents (runtime picker) ───────────────────────────────────────────────


@app.get("/api/agents")
async def list_agents(request: Request):
    """Probe each configured runtime's /api/info; return what's reachable."""
    http: httpx.AsyncClient = request.app.state.http
    results = []
    for url in _AGENT_RUNTIMES:
        entry: Dict[str, Any] = {"url": url, "healthy": False}
        try:
            resp = await http.get(f"{url}/api/info", timeout=3.0)
            resp.raise_for_status()
            entry.update(resp.json())
            entry["healthy"] = True
        except Exception as e:  # noqa: BLE001 — surface every probe failure
            entry["error"] = str(e)
        results.append(entry)
    return results


# ── Sessions ──────────────────────────────────────────────────────────────
# JS owns session IDs and messages content; the server is dumb storage plus
# a proxy to the runtime. All session mutations go through PUT (upsert).


def _storage(request: Request) -> ChatHistoryBackend:
    return request.app.state.storage


@app.get("/api/sessions")
async def list_sessions(request: Request, user: UserIdentity = Depends(require_user)):
    return await _storage(request).list_sessions(user_id=user.user_id)


@app.post("/api/sessions")
async def create_session(request: Request, user: UserIdentity = Depends(require_user)):
    """Create a new empty session bound to the given agent.

    Body: {"agent_url"?: str, "agent_name"?: str}. Missing fields fall back
    to the first configured runtime.
    """
    body = await request.json()
    agent_url = body.get("agent_url") or (_AGENT_RUNTIMES[0] if _AGENT_RUNTIMES else None)
    agent_name = body.get("agent_name") or "agent"
    if not agent_url:
        raise HTTPException(400, "no agent_url and no AGENT_RUNTIMES configured")
    now = _now_iso()
    session = {
        "id": uuid.uuid4().hex[:12],
        "agent_name": agent_name,
        "agent_url": agent_url,
        "title": "New chat",
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }
    await _storage(request).save_session(session, user_id=user.user_id)
    return session


@app.get("/api/sessions/{session_id}")
async def get_session(
    session_id: str, request: Request, user: UserIdentity = Depends(require_user)
):
    s = await _storage(request).get_session(session_id, user_id=user.user_id)
    if s is None:
        raise HTTPException(404, "session not found")
    return s


@app.put("/api/sessions/{session_id}")
async def put_session(
    session_id: str, request: Request, user: UserIdentity = Depends(require_user)
):
    """Upsert a full session. Body is the session dict; id in path wins."""
    body = await request.json()
    body["id"] = session_id
    body.setdefault("created_at", _now_iso())
    body["updated_at"] = _now_iso()
    body.setdefault("messages", [])
    if "agent_url" not in body or "agent_name" not in body:
        raise HTTPException(400, "agent_url and agent_name are required")
    await _storage(request).save_session(body, user_id=user.user_id)
    return body


@app.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: str, request: Request, user: UserIdentity = Depends(require_user)
):
    await _storage(request).delete_session(session_id, user_id=user.user_id)
    return {"status": "deleted"}


# ── Chat proxy ────────────────────────────────────────────────────────────


# Anthropic is strict: tool_use blocks may only contain type/id/name/input.
# The frontend stores extra UI-only fields (e.g. `args_summary` for the
# collapsed tool-card label) — strip them before forwarding.
_TOOL_USE_ALLOWED = {"type", "id", "name", "input"}


def _strip_ui_fields(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a copy of `messages` with UI-only fields removed.

    - Drops the top-level `meta` field (token usage / stop_reason).
    - Drops unknown keys from `tool_use` content blocks (e.g. args_summary).
    """
    cleaned: List[Dict[str, Any]] = []
    for m in messages:
        m = {k: v for k, v in m.items() if k != "meta"}
        content = m.get("content")
        if isinstance(content, list):
            new_content = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    block = {k: v for k, v in block.items() if k in _TOOL_USE_ALLOWED}
                new_content.append(block)
            m["content"] = new_content
        cleaned.append(m)
    return cleaned


@app.post("/api/sessions/{session_id}/chat")
async def chat(
    session_id: str, request: Request, user: UserIdentity = Depends(require_user)
):
    """Proxy POST to `{session.agent_url}/api/chat`, streaming the SSE back.

    Request body: {"messages": [...full history, ending in the new user
    turn...], "trace_id"?: str}. Storage isn't mutated here — the browser
    PUTs the completed session back to /api/sessions/{id} when the stream
    ends.
    """
    session = await _storage(request).get_session(session_id, user_id=user.user_id)
    if session is None:
        raise HTTPException(404, "session not found")
    body = await request.json()
    messages = _strip_ui_fields(body.get("messages", []))
    if not messages:
        raise HTTPException(400, "messages must be a non-empty array")
    trace_id = body.get("trace_id") or uuid.uuid4().hex

    http: httpx.AsyncClient = request.app.state.http
    target = f"{session['agent_url']}/api/chat"

    # Forward the user's ID token as Bearer so the runtime can validate
    # and extract an authoritative UserIdentity. Frontend-derived identity
    # is trusted here (behind EasyAuth) but runtime re-validates for
    # defense in depth + for the "direct service caller" path.
    forward_headers = {"Accept": "text/event-stream"}
    if user.raw_token:
        forward_headers["Authorization"] = f"Bearer {user.raw_token}"

    async def upstream() -> AsyncIterator[bytes]:
        # httpx streaming context manager: forward every chunk as-is.
        async with http.stream(
            "POST",
            target,
            json={
                "messages": messages,
                "trace_id": trace_id,
                "conversation_id": session_id,
            },
            headers=forward_headers,
        ) as r:
            if r.status_code != 200:
                err = (await r.aread()).decode("utf-8", errors="replace")
                raise HTTPException(r.status_code, f"runtime returned {r.status_code}: {err}")
            async for chunk in r.aiter_raw():
                yield chunk

    return StreamingResponse(
        upstream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering if any
        },
    )


@app.post("/api/sessions/{session_id}/confirm/{request_id}")
async def confirm(
    session_id: str,
    request_id: str,
    request: Request,
    user: UserIdentity = Depends(require_user),
):
    """Forward HITL allow/deny to the session's runtime."""
    session = await _storage(request).get_session(session_id, user_id=user.user_id)
    if session is None:
        raise HTTPException(404, "session not found")
    body = await request.json()
    http: httpx.AsyncClient = request.app.state.http
    forward_headers = {}
    if user.raw_token:
        forward_headers["Authorization"] = f"Bearer {user.raw_token}"
    try:
        resp = await http.post(
            f"{session['agent_url']}/api/confirm/{request_id}",
            json={"allowed": bool(body.get("allowed", False))},
            headers=forward_headers,
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"runtime unreachable: {e}") from e
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


# ── Read-only meta passthrough ────────────────────────────────────────────
# Convenience so the UI can fetch skill lists etc. without embedding a
# runtime URL. Always routes to the first configured runtime — fine for
# now since skills/tools are typically the same across replicas.


async def _get_first_runtime(request: Request, path: str) -> Response:
    if not _AGENT_RUNTIMES:
        raise HTTPException(503, "no runtimes configured")
    http: httpx.AsyncClient = request.app.state.http
    try:
        resp = await http.get(f"{_AGENT_RUNTIMES[0]}{path}", timeout=10.0)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"runtime unreachable: {e}") from e
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@app.get("/api/tools")
async def tools(request: Request):
    return await _get_first_runtime(request, "/api/tools")


@app.get("/api/skills")
async def skills(request: Request):
    return await _get_first_runtime(request, "/api/skills")


@app.get("/api/skills/{name}")
async def skill_content(name: str, request: Request):
    return await _get_first_runtime(request, f"/api/skills/{name}")
