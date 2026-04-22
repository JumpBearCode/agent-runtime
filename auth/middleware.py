"""Tool-call auth middleware.

Call site: agent_runtime/core/tools.py, right before handing off to the
MCP client. `inject_auth_for_mcp` figures out which provider is bound
(via auth.json) to the MCP server this tool belongs to, fetches a
valid token, and returns the kwargs to inject into the tool call.

Keeps auth logic out of tools.py — tools.py only knows "ask this module
for the auth kwargs, merge them into args".
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .config import AuthConfig
from .context import current_user
from .providers import AuthRequired, get_provider

logger = logging.getLogger(__name__)


def mcp_server_from_qualified_name(qualified: str) -> Optional[str]:
    """Extract the MCP server name from a qualified tool name of form
    `mcp_{server}_{tool}`. Returns None if the name is not MCP-qualified."""
    if not qualified.startswith("mcp_"):
        return None
    # Format is mcp_<server>_<tool>. Server name may contain underscores;
    # we can't split perfectly without knowing registered server names,
    # so the caller (which has the MCPManager) should prefer to pass the
    # server name directly. This helper is for test/introspection.
    rest = qualified[4:]
    parts = rest.split("_", 1)
    return parts[0] if parts else None


def inject_auth_for_mcp(
    cfg: AuthConfig,
    mcp_server_name: str,
) -> dict[str, Any]:
    """Return the kwargs to merge into the MCP tool args, or {} if this
    MCP server has no provider binding.

    May raise AuthRequired (device_code cache miss). The caller lifts
    that into the frontend's SSE stream via the device-flow pending
    primitive (implemented in PR4).
    """
    provider_name = cfg.provider_for_mcp(mcp_server_name)
    if not provider_name:
        return {}

    provider = get_provider(provider_name)

    user = current_user()
    user_id = user.user_id if (user and provider.mode == "device_code") else None

    try:
        rec = provider.get_valid_token(user_id)
    except AuthRequired:
        raise
    except Exception as e:
        # Provider blew up for a non-device reason (network, misconfig).
        # Log and propagate — tool call will report an error to the LLM.
        logger.exception("provider %r failed to fetch token: %s", provider_name, e)
        raise

    return {
        "_auth_token":       rec.token,
        "_auth_expires_at":  int(rec.expires_at),
    }
