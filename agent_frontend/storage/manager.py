"""Backend factory — pick storage by env var.

CHAT_STORAGE = "local"   → SQLite at CHAT_SQLITE_PATH (default ./agent_frontend.db)
CHAT_STORAGE = "postgres" → PostgresBackend with CHAT_POSTGRES_URL

Patterned after generic-ai infrastructure/manager.py, simplified to the
two-backend case (no write-through cache — we're single-process).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .base import ChatHistoryBackend
from .local import SQLiteBackend
from .postgres import PostgresBackend

logger = logging.getLogger(__name__)


async def create_backend() -> ChatHistoryBackend:
    """Construct and connect the configured backend."""
    mode = os.getenv("CHAT_STORAGE", "local").lower()

    if mode == "local":
        path = os.getenv("CHAT_SQLITE_PATH", "./agent_frontend.db")
        backend = SQLiteBackend()
        await backend.connect(path=path)
        logger.info("Chat storage: SQLite (%s)", path)
        return backend

    if mode == "postgres":
        conn = os.environ.get("CHAT_POSTGRES_URL")
        if not conn:
            raise RuntimeError(
                "CHAT_STORAGE=postgres requires CHAT_POSTGRES_URL (e.g. "
                "postgresql://user:pass@host:5432/db)"
            )
        backend = PostgresBackend()
        await backend.connect(connection_string=conn)
        logger.info("Chat storage: PostgreSQL")
        return backend

    raise ValueError(f"Unknown CHAT_STORAGE={mode!r}; expected 'local' or 'postgres'")


# Convenience: module-level singleton used by server.py's lifespan.
_backend: Optional[ChatHistoryBackend] = None


async def get_backend() -> ChatHistoryBackend:
    global _backend
    if _backend is None:
        _backend = await create_backend()
    return _backend


async def close_backend() -> None:
    global _backend
    if _backend is not None:
        await _backend.close()
        _backend = None
