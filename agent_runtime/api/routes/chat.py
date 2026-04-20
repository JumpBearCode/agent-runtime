"""Chat — stateless SSE streaming endpoint.

The frontend sends the full conversation history with every request. The
runtime never persists; it computes one round and returns events.
"""

import json

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat")
async def chat(request: Request):
    """Run one agent round.

    Request body:
        {
          "messages":  [...],                    # full history; last item must be a user message
          "trace_id":  "optional-uuid"           # scopes HITL confirms; auto-generated if omitted
        }

    Response: text/event-stream — see api.schemas for event shapes.
    """
    body = await request.json()
    messages = body.get("messages")
    trace_id = body.get("trace_id")

    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="`messages` must be a non-empty array")
    if messages[-1].get("role") != "user":
        raise HTTPException(status_code=400, detail="last message must have role=user")

    engine = request.app.state.engine

    async def event_stream():
        async for event in engine.chat_stream(messages, trace_id=trace_id):
            yield {
                "event": event.type,
                "data": json.dumps(event.to_dict(), ensure_ascii=False),
            }

    return EventSourceResponse(event_stream())
