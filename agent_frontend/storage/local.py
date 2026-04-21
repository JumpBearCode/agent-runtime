"""SQLite-backed chat history — zero-config local storage.

One row per session. Messages serialized as a JSON text column.
The SQLite file is the whole store; delete it to reset.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from .base import ChatHistoryBackend

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT 'local',
    agent_name  TEXT NOT NULL,
    agent_url   TEXT NOT NULL,
    title       TEXT,
    messages    TEXT NOT NULL,                    -- JSON array
    created_at  TEXT NOT NULL,                    -- ISO 8601 UTC
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
    ON sessions (user_id, updated_at DESC);
"""


class SQLiteBackend(ChatHistoryBackend):
    def __init__(self) -> None:
        self._path: Optional[Path] = None
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self, path: str = "./agent_frontend.db") -> None:
        self._path = Path(path).expanduser().resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._path))
        # executescript handles the multi-statement _SCHEMA in one call.
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("SQLite chat history opened at %s", self._path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def list_sessions(self, user_id: str = "local") -> List[Dict[str, Any]]:
        assert self._db is not None, "connect() first"
        async with self._db.execute(
            """
            SELECT id, agent_name, agent_url, title, created_at, updated_at
            FROM sessions
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "agent_name": r[1],
                "agent_url": r[2],
                "title": r[3],
                "created_at": r[4],
                "updated_at": r[5],
            }
            for r in rows
        ]

    async def get_session(self, session_id: str, user_id: str = "local") -> Optional[Dict[str, Any]]:
        assert self._db is not None, "connect() first"
        async with self._db.execute(
            """
            SELECT id, agent_name, agent_url, title, messages, created_at, updated_at
            FROM sessions
            WHERE user_id = ? AND id = ?
            """,
            (user_id, session_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "agent_name": row[1],
            "agent_url": row[2],
            "title": row[3],
            "messages": json.loads(row[4]),
            "created_at": row[5],
            "updated_at": row[6],
        }

    async def save_session(self, session: Dict[str, Any], user_id: str = "local") -> None:
        assert self._db is not None, "connect() first"
        await self._db.execute(
            """
            INSERT INTO sessions (id, user_id, agent_name, agent_url, title,
                                  messages, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, id) DO UPDATE SET
                agent_name = excluded.agent_name,
                agent_url  = excluded.agent_url,
                title      = excluded.title,
                messages   = excluded.messages,
                updated_at = excluded.updated_at
            """,
            (
                session["id"],
                user_id,
                session["agent_name"],
                session["agent_url"],
                session.get("title"),
                json.dumps(session.get("messages", []), ensure_ascii=False),
                session["created_at"],
                session["updated_at"],
            ),
        )
        await self._db.commit()

    async def delete_session(self, session_id: str, user_id: str = "local") -> None:
        assert self._db is not None, "connect() first"
        await self._db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND id = ?",
            (user_id, session_id),
        )
        await self._db.commit()
