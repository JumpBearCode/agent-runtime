"""Global configuration — initialized by main() via _setup_workspace()."""

import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)


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
