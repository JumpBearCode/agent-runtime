"""FastAPI app — composed in agent_runtime so the runtime ships as one container.

Run with:
    uvicorn agent_runtime.api.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

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


class PrivateNetworkAccessMiddleware(BaseHTTPMiddleware):
    """Opt in to Chrome's Private Network Access so browsers on public-address
    origins (including localhost:3000 in some Chrome heuristics) can reach
    this runtime on a loopback/private address.

    Sends `Access-Control-Allow-Private-Network: true` on both the preflight
    response and the actual response. Without this, Chrome silently drops the
    response body mid-stream with net::ERR_ABORTED.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response


_cors_origins = os.getenv("AGENT_CORS_ORIGINS", "*").split(",")
# Starlette: last add_middleware = outermost. We want PNA outermost so its
# header is tacked onto every response including CORS-generated preflight 200s.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)
app.add_middleware(PrivateNetworkAccessMiddleware)

app.include_router(meta.router)
app.include_router(chat.router)
app.include_router(confirm.router)
