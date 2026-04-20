"""Read-only metadata endpoints — info, healthz, tools, skills."""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/info")
async def info(request: Request):
    return request.app.state.engine.info


@router.get("/tools")
async def tools(request: Request):
    return request.app.state.engine.get_tools()


@router.get("/skills")
async def skills(request: Request):
    """Map of {name: description}."""
    return request.app.state.engine.get_skills()


@router.get("/skills/{name}")
async def skill_content(name: str, request: Request):
    """Full body of one skill — frontend appends this to its messages list
    when the user explicitly invokes a skill.
    """
    content = request.app.state.engine.get_skill_content(name)
    if content is None:
        raise HTTPException(status_code=404, detail=f"unknown skill: {name}")
    return {"name": name, "content": content}
