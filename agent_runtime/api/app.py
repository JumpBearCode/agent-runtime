"""FastAPI app — composed in agent_runtime so the runtime ships as one container.

Run with:
    uvicorn agent_runtime.api.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..engine import AgentEngine
from .routes import chat, confirm, meta

# Container-friendly logging: structured prefix, no ANSI, level via env.
# `force=True` so we win over uvicorn's default handler when imported as
# `uvicorn agent_runtime.api.app:app`.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    force=True,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Construct the singleton engine on startup; tear it down on shutdown.

    One engine per uvicorn worker process. Horizontal scale = more workers.
    """
    logger.info("Starting AgentEngine")
    app.state.engine = AgentEngine()
    logger.info("AgentEngine ready: %s", app.state.engine.info)
    try:
        yield
    finally:
        logger.info("Shutting down AgentEngine")
        app.state.engine.shutdown()


app = FastAPI(title="Agent Runtime", lifespan=lifespan)

_cors_origins = os.getenv("AGENT_CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

app.include_router(meta.router)
app.include_router(chat.router)
app.include_router(confirm.router)
