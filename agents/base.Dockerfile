# Base image for any agent-runtime container.
#
# Build:
#   docker build -f agents/base.Dockerfile -t agent-runtime-base:0.1 .
#
# Per-agent images extend this and COPY in their own skills/, settings/,
# prompts/, and (optional) mcp/ directories.

FROM python:3.12-slim AS builder

# uv (fast Python package manager) for the build stage.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Install dependencies into a venv that we'll copy into the runtime image.
COPY pyproject.toml uv.lock ./
COPY agent_runtime ./agent_runtime
RUN uv sync --frozen --no-dev --no-install-workspace \
    && uv pip install --python /build/.venv/bin/python --no-deps .


FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash agent

WORKDIR /app

# Bring the built venv + the runtime package over.
COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/agent_runtime /app/agent_runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    AGENT_WORKDIR=/app \
    AGENT_SETTINGS_DIR=/app/settings \
    AGENT_SKILLS_DIR=/app/skills \
    AGENT_SYSTEM_PROMPT_FILE=/app/prompts/system.md

# Per-agent images add: COPY skills/ settings/ prompts/ mcp/ → /app/...

USER agent

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/healthz || exit 1

CMD ["uvicorn", "agent_runtime.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
