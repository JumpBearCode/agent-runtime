"""HITL confirm response — frontend POSTs here when user clicks Allow/Deny."""

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import UserIdentity

from ..deps import require_user

router = APIRouter(prefix="/api/confirm", tags=["confirm"])


@router.post("/{request_id}")
async def respond_confirm(
    request_id: str,
    request: Request,
    user: UserIdentity = Depends(require_user),
):
    body = await request.json()
    allowed = bool(body.get("allowed", False))
    resolved = request.app.state.engine.respond_confirm(request_id, allowed)
    if not resolved:
        # Slot already timed out, was cancelled, or never existed.
        raise HTTPException(status_code=410, detail="confirm request no longer pending")
    return {"status": "ok", "allowed": allowed}
