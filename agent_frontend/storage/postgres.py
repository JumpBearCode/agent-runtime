"""PostgreSQL-backed chat history — production storage.

Same shape as SQLite backend: one row per session, messages as JSONB.
Uses asyncpg connection pool.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from .base import ChatHistoryBackend

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT 'local',
    agent_name  TEXT NOT NULL,
    agent_url   TEXT NOT NULL,
    title       TEXT,
    messages    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
    ON sessions (user_id, updated_at DESC);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class PostgresBackend(ChatHistoryBackend):
    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self, connection_string: str, min_size: int = 1, max_size: int = 10) -> None:
        self._pool = await asyncpg.create_pool(
            connection_string, min_size=min_size, max_size=max_size
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        logger.info("PostgreSQL chat history connected")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def list_sessions(self, user_id: str = "local") -> List[Dict[str, Any]]:
        assert self._pool is not None, "connect() first"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, agent_name, agent_url, title, created_at, updated_at
                FROM sessions
                WHERE user_id = $1
                ORDER BY updated_at DESC
                """,
                user_id,
            )
        return [
            {
                "id": r["id"],
                "agent_name": r["agent_name"],
                "agent_url": r["agent_url"],
                "title": r["title"],
                "created_at": _iso(r["created_at"]),
                "updated_at": _iso(r["updated_at"]),
            }
            for r in rows
        ]

    async def get_session(self, session_id: str, user_id: str = "local") -> Optional[Dict[str, Any]]:
        assert self._pool is not None, "connect() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, agent_name, agent_url, title, messages,
                       created_at, updated_at
                FROM sessions
                WHERE user_id = $1 AND id = $2
                """,
                user_id,
                session_id,
            )
        if row is None:
            return None
        # asyncpg returns JSONB as str; parse it.
        messages = row["messages"]
        if isinstance(messages, str):
            messages = json.loads(messages)
        return {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "agent_url": row["agent_url"],
            "title": row["title"],
            "messages": messages,
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    async def save_session(self, session: Dict[str, Any], user_id: str = "local") -> None:
        assert self._pool is not None, "connect() first"
        created_at = datetime.fromisoformat(session["created_at"])
        updated_at = datetime.fromisoformat(session["updated_at"])
        messages_json = json.dumps(session.get("messages", []), ensure_ascii=False)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sessions (id, user_id, agent_name, agent_url, title,
                                      messages, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                ON CONFLICT (user_id, id) DO UPDATE SET
                    agent_name = EXCLUDED.agent_name,
                    agent_url  = EXCLUDED.agent_url,
                    title      = EXCLUDED.title,
                    messages   = EXCLUDED.messages,
                    updated_at = EXCLUDED.updated_at
                """,
                session["id"],
                user_id,
                session["agent_name"],
                session["agent_url"],
                session.get("title"),
                messages_json,
                created_at,
                updated_at,
            )

    async def delete_session(self, session_id: str, user_id: str = "local") -> None:
        assert self._pool is not None, "connect() first"
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE user_id = $1 AND id = $2",
                user_id,
                session_id,
            )
