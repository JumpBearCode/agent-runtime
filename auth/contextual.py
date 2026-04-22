"""ContextualCredential — Azure SDK credential reading from thread-local.

Why this exists: MCP subprocesses are long-lived and shared across users.
We want one Azure SDK client (e.g. DataFactoryManagementClient) in the
subprocess, not one per user. But each tool invocation needs to run with
the calling user's token (device_code mode) or the container's token
(service mode).

Solution: the client holds a ContextualCredential that reads from a
threading.local each time the SDK calls .get_token(). The tool function
sets the local before calling the SDK and clears it in a finally block.
The subprocess itself never caches user tokens; ContextualCredential is
a thread-local pass-through.

Conforms to azure.core.credentials.TokenCredential by implementing
get_token(*scopes, **kwargs) -> AccessToken.
"""

from __future__ import annotations

import functools
import threading
from typing import Any, Callable, Optional

# Imported lazily so non-Azure MCP servers don't pay the import cost.
try:
    from azure.core.credentials import AccessToken
    _HAS_AZURE = True
except ImportError:  # pragma: no cover — Azure SDK optional in non-Azure MCPs
    _HAS_AZURE = False
    AccessToken = None  # type: ignore


_local = threading.local()


class ContextualCredential:
    """Azure TokenCredential that returns the current thread's token.

    Usage:
        cred = ContextualCredential()
        client = SomeAzureClient(cred, subscription_id)   # long-lived

        # per call:
        ContextualCredential.set_token(token, expires_at)
        try:
            client.do_thing()
        finally:
            ContextualCredential.clear()
    """

    @staticmethod
    def set_token(token: str, expires_at: int) -> None:
        _local.token = token
        _local.expires_at = int(expires_at)

    @staticmethod
    def clear() -> None:
        for attr in ("token", "expires_at"):
            if hasattr(_local, attr):
                delattr(_local, attr)

    def get_token(self, *scopes: str, **kwargs: Any):  # noqa: ARG002
        if not _HAS_AZURE:
            raise RuntimeError(
                "ContextualCredential requires azure-core — install the "
                "Azure SDK or use a non-Azure credential path"
            )
        token: Optional[str] = getattr(_local, "token", None)
        if not token:
            raise RuntimeError(
                "ContextualCredential: no token in thread-local. The "
                "runtime middleware must call set_token() before the "
                "SDK call, or the tool forgot to use @with_auth."
            )
        expires_at: int = getattr(_local, "expires_at", 0)
        return AccessToken(token, expires_at)


def with_auth(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for MCP tool functions. Pulls `_auth_token` and
    `_auth_expires_at` from the tool's kwargs, sets them on the
    ContextualCredential thread-local for the duration of the call,
    and clears on exit.

    The runtime middleware injects these kwargs before each call so the
    LLM never sees them in the tool schema.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, _auth_token: Optional[str] = None,
                _auth_expires_at: Optional[int] = None, **kwargs: Any) -> Any:
        set_locally = False
        if _auth_token:
            ContextualCredential.set_token(_auth_token, _auth_expires_at or 0)
            set_locally = True
        try:
            return fn(*args, **kwargs)
        finally:
            if set_locally:
                ContextualCredential.clear()

    return wrapper
