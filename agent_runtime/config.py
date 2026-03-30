"""Global configuration — initialized by main() via _setup_workspace()."""

import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# Set by _setup_workspace() at startup
WORKDIR = Path.cwd()
SANDBOX_ENABLED = False
SANDBOX_MODE = "ephemeral"  # "ephemeral" (remove on exit) or "persistent" (keep running)
CONTAINER_NAME = "agent-sandbox"
SANDBOX_IMAGE = "agent-sandbox"

# Compression settings
COMPACT_THRESHOLD = 50000
KEEP_RECENT = 3

# Thinking — set via --thinking flag
THINKING_ENABLED = False
THINKING_BUDGET = 10000  # max tokens for thinking per turn
