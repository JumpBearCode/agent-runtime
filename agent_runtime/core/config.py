"""Global configuration — env-driven for container deployment.

Container-friendly env vars:
  AGENT_WORKDIR             — workspace dir for tool file IO. Default: cwd
  AGENT_SETTINGS_DIR        — folder containing mcp.json + HITL.json. Default: $AGENT_WORKDIR/.agent_settings
  AGENT_SKILLS_DIR          — folder containing skill subdirs with SKILL.md. Default: $AGENT_WORKDIR/skills
  AGENT_SYSTEM_PROMPT_FILE  — optional path to a system-prompt template. Default: $AGENT_WORKDIR/prompts/system.md (loaded if present)
  AGENT_HITL_TIMEOUT        — seconds the runtime will wait for a HITL approval. Default: 600
  AGENT_MAX_CONCURRENT_CHATS — ThreadPoolExecutor max_workers. Default: 64
  TOOL_OUTPUT_LIMIT         — max chars per tool result. Default: 10000
  MODEL_ID                  — Anthropic model id. Required.
  ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL / ANTHROPIC_FOUNDRY_*

Settings precedence is intentionally flat — there's no longer a CWD walk.
The container holds exactly one config; mount/copy what you want and point
AGENT_* at it.
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

# ── workspace + per-agent paths ────────────────────────────────────────────

WORKDIR = Path(os.getenv("AGENT_WORKDIR", str(Path.cwd()))).resolve()
WORKDIR.mkdir(parents=True, exist_ok=True)

SETTINGS_DIR = Path(os.getenv("AGENT_SETTINGS_DIR", str(WORKDIR / ".agent_settings"))).resolve()
SKILLS_DIR = Path(os.getenv("AGENT_SKILLS_DIR", str(WORKDIR / "skills"))).resolve()

_default_prompt_file = WORKDIR / "prompts" / "system.md"
SYSTEM_PROMPT_FILE = os.getenv("AGENT_SYSTEM_PROMPT_FILE")
if SYSTEM_PROMPT_FILE is None and _default_prompt_file.is_file():
    SYSTEM_PROMPT_FILE = str(_default_prompt_file)

# ── runtime knobs ──────────────────────────────────────────────────────────

HITL_TIMEOUT = int(os.getenv("AGENT_HITL_TIMEOUT", "600"))
MAX_CONCURRENT_CHATS = int(os.getenv("AGENT_MAX_CONCURRENT_CHATS", "64"))
TOOL_OUTPUT_LIMIT = int(os.getenv("TOOL_OUTPUT_LIMIT", "10000"))

# Thinking
THINKING_ENABLED = os.getenv("AGENT_THINKING", "0") == "1"
THINKING_BUDGET = int(os.getenv("AGENT_THINKING_BUDGET", "10000"))

# Set to True by engine after wiring the confirm hook.
CONFIRM = False


# ---------------------------------------------------------------------------
# Settings resolution — single layer (the configured SETTINGS_DIR).
# ---------------------------------------------------------------------------

def resolve_mcp_config() -> dict:
    """Read mcp.json from SETTINGS_DIR. Returns {} if missing/invalid."""
    path = SETTINGS_DIR / "mcp.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        logger.warning("%s: invalid JSON — %s", path, e)
        return {}
    servers = data.get("servers", {})
    if not servers:
        return {}
    logger.info("Loading MCP servers from %s", path)
    return {"servers": servers}


def resolve_hitl() -> set[str]:
    """Read HITL.json from SETTINGS_DIR. Returns empty set if missing/invalid."""
    path = SETTINGS_DIR / "HITL.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        logger.warning("%s: invalid JSON — %s", path, e)
        return set()
    if not isinstance(data, list):
        logger.warning("%s: expected a JSON array", path)
        return set()
    return set(data)
