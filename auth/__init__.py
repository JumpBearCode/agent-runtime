"""Top-level auth package — identity, token cache, provider abstractions.

Independent of agent_runtime and agent_frontend so both consume the same
logic. See doc/auth-v1-status.md for scope.
"""

from .identity import UserIdentity, validate_jwt, InvalidToken
from .easyauth import parse_easyauth_headers
from .context import current_user, set_current_user, reset_current_user

__all__ = [
    "UserIdentity",
    "validate_jwt",
    "InvalidToken",
    "parse_easyauth_headers",
    "current_user",
    "set_current_user",
    "reset_current_user",
]
