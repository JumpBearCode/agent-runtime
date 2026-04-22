"""FastAPI dependencies — auth gatekeeping.

`require_user` is the single entry point for identity in the runtime.
Every route that accepts user traffic mounts it. On success the
UserIdentity is also placed in auth.context for the engine/tools to read
via ContextVar — routes that need it as a parameter can still accept the
Depends return value directly.

Critical boundary: this ContextVar set is on the request (asyncio) task.
The engine re-sets it on the worker thread when it hops into the
ThreadPoolExecutor; see engine.chat_stream._run_sync.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Header, HTTPException, status

from auth import UserIdentity, validate_jwt, set_current_user
from auth.identity import InvalidToken

from ..core import config

logger = logging.getLogger(__name__)


def _dev_user() -> UserIdentity:
    return UserIdentity(
        user_id="00000000-0000-0000-0000-000000000001",
        principal_name="dev@example.com",
        display_name="Dev User",
        is_authenticated=True,
        raw_token=None,
    )


async def require_user(
    authorization: Optional[str] = Header(default=None),
) -> UserIdentity:
    """Validate the Bearer token and return the UserIdentity.

    In AUTH_DEV_MODE, bypass validation and return a synthetic dev user.
    Intended for local development; never enable in shared environments.
    """
    if config.AUTH_DEV_MODE:
        user = _dev_user()
        set_current_user(user)
        return user

    if not config.AAD_TENANT_ID or not config.AAD_AUDIENCE:
        # Misconfiguration — refuse rather than silently accept.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth not configured",
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[7:].strip()
    try:
        user = validate_jwt(
            token,
            tenant_id=config.AAD_TENANT_ID,
            audience=config.AAD_AUDIENCE,
            required_scope=config.AAD_REQUIRED_SCOPE,
        )
    except InvalidToken as e:
        logger.info("jwt rejected: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )

    set_current_user(user)
    return user
