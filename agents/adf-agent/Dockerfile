# ADF agent — extends the base runtime with ADF skills, MCP server, and prompt.
#
# Build (from project root):
#   docker build -f agents/base.Dockerfile -t agent-runtime-base:0.1 .
#   docker build -f agents/adf-agent/Dockerfile -t agent-runtime-adf:0.1 .
#
# Run:
#   docker run --rm -p 8001:8000 \
#       -e MODEL_ID=claude-sonnet-4-6 \
#       -e ANTHROPIC_API_KEY=... \
#       -e ADF_SUBSCRIPTION_ID=... \
#       -e ADF_RESOURCE_GROUP=... \
#       -e ADF_FACTORY_NAME=... \
#       agent-runtime-adf:0.1

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

ENV AGENT_NAME=adf

USER agent
