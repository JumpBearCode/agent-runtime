"""Session CRUD — create / list / load / delete chat sessions."""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(request: Request):
    return request.app.state.engine.list_sessions()


@router.post("")
async def create_session(request: Request):
    sid = request.app.state.engine.create_session()
    return {"id": sid}


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request):
    engine = request.app.state.engine
    try:
        history = engine.load_session(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="session not found")

    # Render history into a frontend-friendly shape (text + tool_calls + thinking).
    messages = []
    for msg in history:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            if isinstance(content, str):
                messages.append({"role": "user", "content": content})
            # Skip tool_result messages.
        elif role == "assistant":
            if isinstance(content, list):
                text_parts, thinking_parts, tool_calls = [], [], []
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
                    "tool_calls": tool_calls or None,
                })
            else:
                messages.append({"role": "assistant", "content": str(content)})
    return {"id": session_id, "messages": messages}


@router.delete("/{session_id}")
async def delete_session(session_id: str, request: Request):
    deleted = request.app.state.engine.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
    return {"status": "deleted"}
