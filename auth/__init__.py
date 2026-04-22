"""Top-level auth package — identity, token cache, provider abstractions.

Independent of agent_runtime and agent_frontend so both consume the same
logic. See doc/auth-v1-status.md for scope.
"""

from .identity import UserIdentity, validate_jwt, InvalidToken
from .easyauth import parse_easyauth_headers
from .context import current_user, set_current_user, reset_current_user
from .cache import TokenCache, TokenRecord, default_cache
from .contextual import ContextualCredential, with_auth
from .config import AuthConfig, load_auth_config
from .middleware import inject_auth_for_mcp
from .device_flow import DevicePrompt, emit_prompt, set_prompt_callback
from .providers import (
    AuthRequired,
    AzureDeviceProvider,
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
    # config + middleware
    "AuthConfig",
    "load_auth_config",
    "inject_auth_for_mcp",
    # device flow plumbing
    "DevicePrompt",
    "emit_prompt",
    "set_prompt_callback",
    # providers
    "AuthRequired",
    "Provider",
    "ProviderMode",
    "AzureServiceProvider",
    "AzureDeviceProvider",
    "build_providers",
    "get_provider",
]
