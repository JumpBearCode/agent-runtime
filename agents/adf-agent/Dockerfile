# ADF agent — extends the base runtime with ADF skills, MCP server, and prompt.
#
# Build (from project root):
#   docker build -f agents/base.Dockerfile -t agent-runtime-base:0.1 .
#   docker build -f agents/adf-agent/Dockerfile -t agent-runtime-adf:0.1 .
#
# Run (minimal):
#   docker run --rm -p 8001:8000 \
#       -e MODEL_ID=claude-sonnet-4-6 \
#       -e ANTHROPIC_API_KEY=... \
#       -e ADF_SUBSCRIPTION_ID=... \
#       -e ADF_RESOURCE_GROUP=... \
#       -e ADF_FACTORY_NAME=... \
#       -v "$(pwd)/agents/adf-agent/workspace:/workspace" \
#       agent-runtime-adf:0.1
#
# Run (recommended — pipes the whole repo-root .env into the container so
# LangSmith, Anthropic, ADF, and everything else stays in sync, and mounts
# the host workspace so anything the agent writes shows up locally):
#   docker run --rm -p 8001:8000 \
#       --env-file .env \
#       -v "$(pwd)/agents/adf-agent/workspace:/workspace" \
#       agent-runtime-adf:0.1
#
# To enable LangSmith tracing, add these to .env (or pass via -e):
#       LANGSMITH_TRACING=true
#       LANGSMITH_API_KEY=lsv2_pt_...
#       LANGSMITH_PROJECT=<your-project>

FROM agent-runtime-base:0.1

# Install agent-specific Python deps from this agent's locked pyproject.
# uv resolves once at build time (export → requirements.txt), then plain pip
# installs into the existing base venv. Reproducible: identical inputs give
# identical bytes.
USER root
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --chown=root:root agents/adf-agent/pyproject.toml agents/adf-agent/uv.lock /tmp/deps/
RUN cd /tmp/deps \
    && uv export --frozen --no-hashes --no-dev --no-emit-project --format requirements.txt > requirements.txt \
    && uv pip install --python /app/.venv/bin/python --no-cache -r requirements.txt \
    && rm -rf /tmp/deps /usr/local/bin/uv

# Bake in this agent's identity.
COPY --chown=agent:agent agents/adf-agent/skills   /app/skills
COPY --chown=agent:agent agents/adf-agent/settings /app/settings
COPY --chown=agent:agent agents/adf-agent/prompts  /app/prompts
COPY --chown=agent:agent agents/adf-agent/mcp      /app/mcp

# Azure CLI is pre-installed in the base image — no per-agent work needed.
# Local dev picks up host credentials via the bind-mounted ~/.azure in
# agents/adf-agent/compose.yml; cloud uses Managed Identity.

ENV AGENT_NAME=adf

USER agent
