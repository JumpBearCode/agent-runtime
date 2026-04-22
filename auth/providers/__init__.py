"""Auth providers — one per credential source.

The runtime builds a Provider instance per entry in auth.json, keyed by
name. `get_provider(name)` returns the cached instance. Middleware asks
the provider for a token before every MCP tool call.
"""

from .base import AuthRequired, Provider, ProviderMode
from .azure_service import AzureServiceProvider
from .registry import build_providers, get_provider

__all__ = [
    "AuthRequired",
    "Provider",
    "ProviderMode",
    "AzureServiceProvider",
    "build_providers",
    "get_provider",
]
