"""Top-level auth package — identity, token cache, provider abstractions.

Independent of agent_runtime and agent_frontend so both consume the same
logic. See doc/auth-v1-status.md for scope.
"""

from .identity import UserIdentity, validate_jwt, InvalidToken
from .easyauth import parse_easyauth_headers
from .context import current_user, set_current_user, reset_current_user
from .cache import TokenCache, TokenRecord, default_cache
from .contextual import ContextualCredential, with_auth
from .providers import (
    AuthRequired,
    AzureServiceProvider,
    Provider,
    ProviderMode,
    build_providers,
    get_provider,
)

__all__ = [
    # identity
    "UserIdentity",
    "validate_jwt",
    "InvalidToken",
    "parse_easyauth_headers",
    # context
    "current_user",
    "set_current_user",
    "reset_current_user",
    # cache
    "TokenCache",
    "TokenRecord",
    "default_cache",
    # contextual credential
    "ContextualCredential",
    "with_auth",
    # providers
    "AuthRequired",
    "Provider",
    "ProviderMode",
    "AzureServiceProvider",
    "build_providers",
    "get_provider",
]
