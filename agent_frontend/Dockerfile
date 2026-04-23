# Frontend container — serves the web UI + chat history storage.
#
# Build (from project root):
#   docker build -f agent_frontend/Dockerfile -t agent-frontend:0.1 .
#
# Normally you don't build this by hand — `./local.sh up` drives it via
# docker-compose.yml at the repo root.
#
# Runtime layout inside the container:
#   /app               — baked: runtime + frontend code, venv, etc.
#   /app/data          — mutable: SQLite chat history (mount a host dir here)

FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Build the venv at /app/.venv (not /build/.venv) so that `uv pip install .`
# bakes the correct interpreter path into entry-script shebangs — we copy
# the venv to /app/.venv in the runtime stage, so builder-stage path must
# match. Otherwise `agent-web` ends up with #!/build/.venv/bin/python and
# exec fails at runtime with "no such file or directory".
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY agent_runtime ./agent_runtime
COPY agent_frontend ./agent_frontend
RUN uv sync --frozen --no-dev --extra frontend --no-install-workspace \
    && uv pip install --python /app/.venv/bin/python --no-deps .


FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash agent \
    && mkdir -p /app/data \
    && chown agent:agent /app/data

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/agent_frontend /app/agent_frontend

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    CHAT_STORAGE=local \
    CHAT_SQLITE_PATH=/app/data/chat.db \
    AGENT_RUNTIMES=""

VOLUME ["/app/data"]

USER agent

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8080/api/healthz || exit 1

# agent-web is the CLI entry point declared in pyproject.toml
# ([project.scripts]). It accepts --host / --port / --reload.
CMD ["agent-web", "--host", "0.0.0.0", "--port", "8080"]
