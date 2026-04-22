"""Provider registry — instantiates one Provider per auth.json entry.

`build_providers(cfg)` is called once at runtime startup with the parsed
auth.json. `get_provider(name)` returns the built instance. Unknown
names raise KeyError so misconfiguration fails loud, not silent.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Provider

logger = logging.getLogger(__name__)


_providers: dict[str, Provider] = {}


def build_providers(providers_cfg: dict[str, dict[str, Any]]) -> dict[str, Provider]:
    """Build and register providers from the `providers` block of auth.json.

    Re-entrant: calling again replaces the previous registry. Tests rely
    on this to reset between cases.
    """
    from .azure_service import AzureServiceProvider

    global _providers
    built: dict[str, Provider] = {}

    for name, cfg in providers_cfg.items():
        mode = cfg.get("mode", "service")
        if mode == "service":
            # For v1 only Azure service mode exists. Future: non-Azure
            # service modes (Snowflake service account, ADO PAT) dispatch
            # by a `type` field.
            scope = cfg.get("scope", "https://management.azure.com/.default")
            built[name] = AzureServiceProvider(name, scope=scope)
        elif mode == "device_code":
            # Device-code providers land in PR4. Register a stub for now
            # so auth.json loaders can validate shape without crashing.
            logger.warning(
                "provider %r is device_code mode but PR4 is not yet merged — "
                "get_valid_token will fail at runtime", name,
            )
        else:
            raise ValueError(f"provider {name!r}: unknown mode {mode!r}")

    _providers = built
    logger.info("auth providers built: %s", sorted(built.keys()))
    return built


def get_provider(name: str) -> Provider:
    try:
        return _providers[name]
    except KeyError:
        raise KeyError(
            f"no auth provider named {name!r} — check auth.json "
            f"(providers or mcp_bindings). Known: {sorted(_providers)}"
        )


def clear_providers() -> None:
    """Test helper — drops every registered provider."""
    global _providers
    _providers = {}
