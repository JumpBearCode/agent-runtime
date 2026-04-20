"""Session persistence — JSONL append + rebuild."""

import json
import time
import uuid
from pathlib import Path

from . import config


class SessionStore:
    """JSONL-based conversation persistence.

    Each session is a single .jsonl file — one line per message.
    Append on every turn, rebuild on load.
    """

    def __init__(self, sessions_dir: Path | None = None):
        self.sessions_dir = sessions_dir or config.SESSIONS_DIR
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.session_id: str | None = None
        self._path: Path | None = None

    # ── lifecycle ──────────────────────────────────────────────

    def new_session(self, session_id: str | None = None) -> str:
        self.session_id = session_id or f"s-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        self._path = self.sessions_dir / f"{self.session_id}.jsonl"
        return self.session_id

    def load_session(self, session_id: str) -> list[dict]:
        """Load a session from disk and return API-compatible messages[]."""
        self.session_id = session_id
        self._path = self.sessions_dir / f"{session_id}.jsonl"
        if not self._path.exists():
            raise FileNotFoundError(f"Session not found: {self._path}")
        return self._rebuild_history(self._path)

    def list_sessions(self) -> list[dict]:
        results = []
        for p in sorted(self.sessions_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            results.append({"id": p.stem, "modified": p.stat().st_mtime, "size": p.stat().st_size})
        return results

    # ── write ──────────────────────────────────────────────────

    def save_turn(self, message: dict):
        """Append one message to the session JSONL file."""
        if not self._path:
            return
        record = self._serialize(message)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    # ── read / rebuild ─────────────────────────────────────────

    def _rebuild_history(self, path: Path) -> list[dict]:
        """Replay JSONL records into API-compatible messages[]."""
        messages: list[dict] = []
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            record = json.loads(line)
            rtype = record.get("type")
            if rtype == "user":
                messages.append({"role": "user", "content": record["content"]})
            elif rtype == "assistant":
                messages.append({"role": "assistant", "content": record["content"]})
        return messages

    # ── serialization helpers ──────────────────────────────────

    def _serialize(self, message: dict) -> dict:
        """Convert a message (possibly containing SDK objects) to a plain dict."""
        role = message["role"]
        content = message["content"]
        ts = int(time.time())

        if role == "user":
            if isinstance(content, str):
                return {"type": "user", "content": content, "ts": ts}
            # list of tool_result dicts or structured blocks
            return {"type": "user", "content": [self._block_to_dict(b) for b in content], "ts": ts}

        if role == "assistant":
            if isinstance(content, str):
                return {"type": "assistant", "content": [{"type": "text", "text": content}], "ts": ts}
            return {"type": "assistant", "content": [self._block_to_dict(b) for b in content], "ts": ts}

        return {"type": role, "content": str(content), "ts": ts}

    @staticmethod
    def _block_to_dict(block) -> dict:
        """Convert an Anthropic SDK content block (or plain dict) to a JSON-safe dict."""
        if isinstance(block, dict):
            return block
        # SDK objects: TextBlock, ToolUseBlock, ThinkingBlock
        if hasattr(block, "type"):
            if block.type == "text":
                return {"type": "text", "text": block.text}
            if block.type == "tool_use":
                return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            if block.type == "thinking":
                return {"type": "thinking", "thinking": block.thinking}
        # fallback — pydantic models
        if hasattr(block, "model_dump"):
            return block.model_dump()
        return {"type": "unknown", "data": str(block)}
