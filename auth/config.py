"""auth.json loader.

Schema:
    {
      "runtime": {                          # optional in v1 (PR1 uses env vars)
        "audience": "api://...",
        "required_scope": "runtime.access"
      },
      "providers": {
        "<name>": {
          "mode": "service" | "device_code",
          ...provider-specific keys...
        }
      },
      "mcp_bindings": {
        "<mcp_server_name>": "<provider_name>"
      }
    }

`mcp_bindings` tells the runtime which provider's token to inject before
dispatching each MCP tool. Unbound MCPs get no token — fine for MCPs that
don't need one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthConfig:
    runtime: dict[str, Any] = field(default_factory=dict)
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcp_bindings: dict[str, str] = field(default_factory=dict)

    def provider_for_mcp(self, mcp_server_name: str) -> Optional[str]:
        return self.mcp_bindings.get(mcp_server_name)


def load_auth_config(path: Path) -> AuthConfig:
    """Parse `auth.json` if present, else return an empty config (no auth
    requirements). Missing file is normal for agents that don't need any
    provider — the middleware becomes a no-op for their MCPs."""
    if not path.is_file():
        logger.info("auth.json not found at %s — using empty config", path)
        return AuthConfig()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        logger.error("auth.json invalid: %s", e)
        raise

    cfg = AuthConfig(
        runtime=data.get("runtime") or {},
        providers=data.get("providers") or {},
        mcp_bindings=data.get("mcp_bindings") or {},
    )

    # Validate mcp_bindings refer to known providers.
    for mcp, prov in cfg.mcp_bindings.items():
        if prov not in cfg.providers:
            raise ValueError(
                f"auth.json: mcp_bindings maps {mcp!r} → {prov!r} but "
                f"no such provider is defined"
            )

    logger.info(
        "auth.json loaded: providers=%s, mcp_bindings=%s",
        sorted(cfg.providers), cfg.mcp_bindings,
    )
    return cfg
