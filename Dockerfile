FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl jq && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Non-root user for sandbox isolation
RUN useradd -m -s /bin/bash agent
USER agent

WORKDIR /workspace

CMD ["sleep", "infinity"]
