"""Abstract chat-history backend interface.

Two concrete backends implement this: SQLite (local dev) and PostgreSQL
(production). The frontend server talks only to this interface so the
storage layer is swappable by a single env var.

A "session" is one chat thread. Its `messages` are Anthropic-shape
content blocks exactly as shipped to the runtime's /api/chat:

    [
      {"role": "user",      "content": "hi"},
      {"role": "assistant", "content": [...blocks...], "meta": {...}},
      {"role": "user",      "content": [{"type":"tool_result", ...}]},
    ]

`meta` is optional and UI-only (token usage, stop_reason). The server
strips it before forwarding to the runtime.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class ChatHistoryBackend(ABC):
    """Persistent store for chat sessions."""

    @abstractmethod
    async def connect(self, **kwargs: Any) -> None:
        """Open the connection / pool / file handle."""

    @abstractmethod
    async def close(self) -> None:
        """Release all resources."""

    @abstractmethod
    async def list_sessions(self, user_id: str = "local") -> List[Dict[str, Any]]:
        """Return sessions ordered by updated_at DESC.

        Each dict has: id, agent_name, agent_url, title, created_at, updated_at.
        Messages are NOT included — fetch via get_session.
        """

    @abstractmethod
    async def get_session(self, session_id: str, user_id: str = "local") -> Optional[Dict[str, Any]]:
        """Return full session dict (including messages), or None if not found."""

    @abstractmethod
    async def save_session(self, session: Dict[str, Any], user_id: str = "local") -> None:
        """Upsert a session by id. Caller owns created_at / updated_at."""

    @abstractmethod
    async def delete_session(self, session_id: str, user_id: str = "local") -> None:
        """Remove a session. No-op if it doesn't exist."""
