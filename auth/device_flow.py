"""Thread-local callback for surfacing device-flow prompts to the frontend.

A device_code provider calls `emit_prompt()` with the verification URL
and user code right after initiating a flow with the IdP. The engine
wires a per-request callback that forwards the payload as an SSE event;
the frontend displays the URL and code so the user can complete login in
a new tab. While the user completes the flow, the provider blocks on the
IdP's token-polling call.

Callback is thread-local because the agent loop runs in a
ThreadPoolExecutor: the engine binds the callback on the worker thread
at the start of each chat_stream turn so different chats never share.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class DevicePrompt:
    provider:         str
    verification_uri: str
    user_code:        str
    expires_in:       int    # seconds until flow expires
    message:          str    # IdP-supplied human-readable instructions


# (thread-local) callback signature: fn(prompt: DevicePrompt) -> None
_local = threading.local()

PromptCallback = Callable[[DevicePrompt], None]


def set_prompt_callback(cb: Optional[PromptCallback]) -> None:
    """Bind the callback on the current thread (typically the agent-loop
    worker). Pass None to clear."""
    _local.cb = cb


def emit_prompt(prompt: DevicePrompt) -> None:
    """Called by a device_code provider to surface a login request. If no
    callback is bound (e.g. during tests or non-chat contexts), silently
    drops — the provider still blocks waiting for the user to complete
    the flow out-of-band."""
    cb: Optional[PromptCallback] = getattr(_local, "cb", None)
    if cb is not None:
        cb(prompt)
