"""FastAPI backend — API routes and SSE streaming."""

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from agent_runtime import config

app = FastAPI(title="Agent Frontend")

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Singleton engine
_engine = None


def get_engine():
    global _engine
    if _engine is None:
        from agent_frontend.engine import AgentEngine, EngineConfig
        _engine = AgentEngine(EngineConfig(
            workspace=str(config.WORKDIR),
            thinking=config.THINKING_ENABLED,
            thinking_budget=config.THINKING_BUDGET,
            settings=config.SETTINGS_OVERRIDE,
            confirm=config.CONFIRM,
        ))
    return _engine


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


@app.get("/api/config")
async def get_config():
    return get_engine().startup_info


@app.get("/api/sessions")
async def list_sessions():
    return get_engine().list_sessions()


@app.post("/api/sessions")
async def create_session():
    sid = get_engine().create_session()
    return {"id": sid}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    engine = get_engine()
    history = engine.load_session(session_id)
    messages = []
    for msg in history:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            if isinstance(content, str):
                messages.append({"role": "user", "content": content})
            # Skip tool_result messages (user messages with list content)
        elif role == "assistant":
            if isinstance(content, list):
                text_parts = []
                thinking_parts = []
                tool_calls = []
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "thinking":
                            thinking_parts.append(block.get("thinking", ""))
                        elif btype == "tool_use":
                            tool_calls.append({"name": block.get("name", ""), "args": block.get("input", {})})
                    elif hasattr(block, "type"):
                        if block.type == "text":
                            text_parts.append(block.text)
                        elif block.type == "thinking":
                            thinking_parts.append(block.thinking)
                        elif block.type == "tool_use":
                            tool_calls.append({"name": block.name, "args": block.input})
                messages.append({
                    "role": "assistant",
                    "content": "\n".join(text_parts),
                    "thinking": "\n".join(thinking_parts) if thinking_parts else None,
                    "tool_calls": tool_calls if tool_calls else None,
                })
            else:
                messages.append({"role": "assistant", "content": str(content)})
    return {"id": session_id, "messages": messages}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    engine = get_engine()
    store = engine.session_store
    session_file = store.sessions_dir / f"{session_id}.jsonl"
    if session_file.exists():
        session_file.unlink()
    if session_id in engine._sessions:
        del engine._sessions[session_id]
    return {"status": "deleted"}


@app.post("/api/sessions/{session_id}/chat")
async def chat(session_id: str, request: Request):
    body = await request.json()
    message = body["message"]
    engine = get_engine()

    async def event_stream():
        async for event in engine.chat_stream(session_id, message):
            yield {"event": event.type, "data": json.dumps(event.to_dict(), ensure_ascii=False)}

    return EventSourceResponse(event_stream())


@app.post("/api/confirm")
async def confirm(request: Request):
    body = await request.json()
    allowed = body.get("allowed", False)
    get_engine().respond_confirm(allowed)
    return {"status": "ok"}


@app.get("/api/tools")
async def list_tools():
    return get_engine().get_tools()


@app.get("/api/skills")
async def list_skills():
    return get_engine().get_skill_names()


@app.post("/api/sessions/{session_id}/skill/{skill_name}")
async def invoke_skill(session_id: str, skill_name: str):
    engine = get_engine()
    content = engine.get_skill_content(skill_name)
    if content is None:
        return JSONResponse({"error": f"Unknown skill: {skill_name}"}, status_code=404)

    async def event_stream():
        async for event in engine.chat_stream(session_id, content):
            yield {"event": event.type, "data": json.dumps(event.to_dict(), ensure_ascii=False)}

    return EventSourceResponse(event_stream())
