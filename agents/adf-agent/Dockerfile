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

# Install ADF MCP server runtime deps (the base image's venv only has
# agent-runtime + its deps — the MCP server needs azure SDKs).
USER root
RUN /app/.venv/bin/pip install --no-cache-dir \
        azure-identity \
        azure-mgmt-datafactory \
        mcp \
        python-dotenv

# Bake in this agent's identity.
COPY --chown=agent:agent agents/adf-agent/skills   /app/skills
COPY --chown=agent:agent agents/adf-agent/settings /app/settings
COPY --chown=agent:agent agents/adf-agent/prompts  /app/prompts
COPY --chown=agent:agent agents/adf-agent/mcp      /app/mcp

USER agent
