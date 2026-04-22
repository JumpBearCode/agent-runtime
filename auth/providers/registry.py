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

    Dispatch key is `(mode, type)`:
      mode=service     → container credential (Azure MI today; future
                          types dispatch per `type` field)
      mode=device_code → per-user interactive flow; `type` picks the IdP

    Re-entrant: calling again replaces the previous registry. Tests rely
    on this to reset between cases.
    """
    from .azure_service import AzureServiceProvider
    from .azure_device import AzureDeviceProvider

    global _providers
    built: dict[str, Provider] = {}

    for name, cfg in providers_cfg.items():
        mode = cfg.get("mode", "service")
        ptype = cfg.get("type", "azure")   # default type is azure for v1

        if mode == "service":
            if ptype != "azure":
                raise ValueError(
                    f"provider {name!r}: service-mode type {ptype!r} not "
                    f"supported in v1 (only azure). See doc/auth-v1-status.md"
                )
            scope = cfg.get("scope", "https://management.azure.com/.default")
            built[name] = AzureServiceProvider(name, scope=scope)

        elif mode == "device_code":
            if ptype == "azure":
                if not cfg.get("tenant") or not cfg.get("client_id"):
                    raise ValueError(
                        f"provider {name!r}: azure device_code requires "
                        f"tenant and client_id"
                    )
                built[name] = AzureDeviceProvider(
                    name,
                    tenant=cfg["tenant"],
                    client_id=cfg["client_id"],
                    scope=cfg.get("scope", "https://management.azure.com/.default"),
                )
            else:
                raise ValueError(
                    f"provider {name!r}: device_code type {ptype!r} not yet "
                    f"supported (expected azure). snowflake/ado land in PR5."
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
