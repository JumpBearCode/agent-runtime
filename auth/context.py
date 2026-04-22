"""Per-request UserIdentity stored in a ContextVar.

Set at request entry (FastAPI dependency), read by engine/tools/auth
cache throughout the request lifetime. Reset on exit to prevent leakage
between requests sharing the same thread (uvicorn thread pool reuse).

Critical: ContextVar does NOT automatically propagate across
`ThreadPoolExecutor.submit` boundaries. The engine must re-set it inside
the worker thread — see agent_runtime/engine.py for the propagation.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

from .identity import UserIdentity

_current_user: ContextVar[Optional[UserIdentity]] = ContextVar(
    "auth_current_user", default=None
)


def current_user() -> Optional[UserIdentity]:
    """Return the current request's UserIdentity, or None if unset."""
    return _current_user.get()


def set_current_user(user: Optional[UserIdentity]) -> Token:
    """Set the UserIdentity for the current context. Returns a token the
    caller uses with `reset_current_user` to restore the prior value."""
    return _current_user.set(user)


def reset_current_user(token: Token) -> None:
    """Restore the prior UserIdentity. Always call in a finally block."""
    _current_user.reset(token)
