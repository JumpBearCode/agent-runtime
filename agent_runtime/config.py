"""Global configuration and three-layer settings resolution.

Settings priority (highest → lowest):
  1. --settings <path>   (CLI override folder)
  2. {WORKDIR}/.agent_settings/
  3. ~/.agent_settings/

MCP:  servers are merged — lower priority as base, higher priority overwrites same-named keys.
HITL: tool names are unioned across all layers.
"""

import json
import logging
import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)


def _create_client():
    """Create the appropriate Anthropic client.

    Azure AI Foundry: set ANTHROPIC_FOUNDRY_API_KEY + ANTHROPIC_FOUNDRY_RESOURCE
                   or ANTHROPIC_FOUNDRY_BASE_URL.
    Standard Anthropic (or compatible providers): set ANTHROPIC_API_KEY
                   and optionally ANTHROPIC_BASE_URL.
    """
    if os.getenv("ANTHROPIC_FOUNDRY_API_KEY") or os.getenv("ANTHROPIC_FOUNDRY_RESOURCE"):
        from anthropic import AnthropicFoundry
        return AnthropicFoundry()

    if os.getenv("ANTHROPIC_BASE_URL"):
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    return Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))


client = _create_client()
MODEL = os.environ["MODEL_ID"]

# Set at startup
WORKDIR = Path.cwd()

# Compression settings
COMPACT_THRESHOLD = 50000
KEEP_RECENT = 3

# Thinking — set via --thinking flag
THINKING_ENABLED = False
THINKING_BUDGET = 10000  # max tokens for thinking per turn

# Settings — set via --settings flag
SETTINGS_OVERRIDE: str | None = None
CONFIRM = False

# ---------------------------------------------------------------------------
# Three-layer settings resolution
# ---------------------------------------------------------------------------

_SETTINGS_DIR = ".agent_settings"


def _candidate_dirs() -> list[Path]:
    """Settings directories in priority order (highest first)."""
    dirs = []
    if SETTINGS_OVERRIDE:
        dirs.append(Path(SETTINGS_OVERRIDE).resolve())
    dirs.append(WORKDIR / _SETTINGS_DIR)
    dirs.append(Path.home() / _SETTINGS_DIR)
    return dirs


def resolve_mcp_config() -> dict:
    """Merge mcp.json servers from all layers.

    Iterates lowest-priority first so higher-priority overwrites same-named servers.
    """
    merged_servers: dict = {}
    for d in reversed(_candidate_dirs()):
        path = d / "mcp.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            logger.warning("%s: invalid JSON — %s", path, e)
            continue
        servers = data.get("servers", {})
        if servers:
            logger.info("Loading MCP servers from %s", path)
            merged_servers.update(servers)
    return {"servers": merged_servers} if merged_servers else {}


def resolve_hitl() -> set[str]:
    """Union HITL.json tool names from all layers."""
    result: set[str] = set()
    for d in _candidate_dirs():
        path = d / "HITL.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            logger.warning("%s: invalid JSON — %s", path, e)
            continue
        if not isinstance(data, list):
            logger.warning("%s: expected a JSON array", path)
            continue
        result.update(data)
    return result
