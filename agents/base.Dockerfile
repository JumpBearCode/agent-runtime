# Base image for any agent-runtime container.
#
# Build:
#   docker build -f agents/base.Dockerfile -t agent-runtime-base:0.1 .
#
# Per-agent images extend this and COPY in their own skills/, settings/,
# prompts/, and (optional) mcp/ directories.

FROM python:3.12-slim-bookworm AS builder

# uv (fast Python package manager) for the build stage.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Install dependencies into a venv that we'll copy into the runtime image.
COPY pyproject.toml uv.lock ./
COPY agent_runtime ./agent_runtime
RUN uv sync --frozen --no-dev --no-install-workspace \
    && uv pip install --python /build/.venv/bin/python --no-deps .


FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && useradd -m -s /bin/bash agent \
    && mkdir -p /workspace \
    && chown agent:agent /workspace \
    # Azure CLI — baked into base because ~90% of agents here are Azure-family
    # (ADF, Fabric, Synapse, …). `DefaultAzureCredential` falls back to
    # `AzureCliCredential` when no Managed Identity is present, which is how
    # local dev auth works (bind-mount host's ~/.azure into the container).
    # In cloud, MI kicks in earlier in the chain and az CLI is never invoked —
    # pure dev overhead (~500MB), zero production impact.
    && curl -sLS https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ $(grep VERSION_CODENAME /etc/os-release | cut -d= -f2) main" \
        > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends azure-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bring the built venv + the runtime package over.
COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/agent_runtime /app/agent_runtime

# Split of concerns:
#   /app       — immutable: runtime code, venv, baked-in skills/settings/prompts/mcp
#   /workspace — mutable: the AI's working directory (mount a host dir here)
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    AGENT_NAME="" \
    AGENT_WORKDIR=/workspace \
    AGENT_SETTINGS_DIR=/app/settings \
    AGENT_SKILLS_DIR=/app/skills \
    AGENT_SYSTEM_PROMPT_FILE=/app/prompts/system.md

VOLUME ["/workspace"]

# Per-agent images add: COPY skills/ settings/ prompts/ mcp/ → /app/...

USER agent

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/healthz || exit 1

CMD ["uvicorn", "agent_runtime.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
