#!/usr/bin/env bash
#
# local.sh — drive the local Docker stack.
#
# Usage:
#   ./local.sh up            # build base image, then docker compose up (foreground)
#   ./local.sh up -d         # same, detached
#   ./local.sh down          # docker compose down
#   ./local.sh logs [svc]    # tail logs
#   ./local.sh ps            # list running services
#   ./local.sh rebuild       # --no-cache rebuild of everything
#   ./local.sh <anything>    # passed straight through to docker compose
#
# For frontend development, run the frontend on the host instead:
#   uv run agent-web --reload     # connects to the dockerized agents
#
# Cloud deploys live under ./infra/ (Bicep). This script is local-only.

set -euo pipefail

# cd to repo root (macOS-compatible — no readlink -f dependency).
cd "$(cd "$(dirname "$0")" && pwd)"

BASE_IMAGE="${BASE_IMAGE:-agent-runtime-base:0.1}"

# ---------------------------------------------------------------------------
# Discover agent fragments.
# Every agent lives at agents/<name>/, and must ship a compose.yml fragment
# alongside its Dockerfile. Adding a new agent is zero-config here —
# drop the directory in and it's picked up automatically.
# ---------------------------------------------------------------------------

AGENT_FRAGMENTS=()
while IFS= read -r -d '' f; do
    AGENT_FRAGMENTS+=("$f")
done < <(find agents -mindepth 2 -maxdepth 2 -name compose.yml -print0 | sort -z)

AGENT_NAMES=()
for f in "${AGENT_FRAGMENTS[@]}"; do
    # agents/<name>/compose.yml → <name>
    name=$(basename "$(dirname "$f")")
    AGENT_NAMES+=("$name")
done

# Compute AGENT_RUNTIMES for the frontend. Service names resolve on the
# internal docker network; every agent's uvicorn listens on :8000 inside
# its container (regardless of the host port mapping).
runtime_urls=()
for name in "${AGENT_NAMES[@]}"; do
    runtime_urls+=("http://${name}:8000")
done
if [ ${#runtime_urls[@]} -gt 0 ]; then
    AGENT_RUNTIMES=$(IFS=,; echo "${runtime_urls[*]}")
else
    AGENT_RUNTIMES=""
fi
export AGENT_RUNTIMES

# Merge root compose + every agent fragment via COMPOSE_FILE.
files=(docker-compose.yml "${AGENT_FRAGMENTS[@]}")
COMPOSE_FILE=$(IFS=:; echo "${files[*]}")
export COMPOSE_FILE

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

build_base() {
    echo "==> Building base image ($BASE_IMAGE) — agents FROM this."
    docker build -f agents/base.Dockerfile -t "$BASE_IMAGE" .
}

print_plan() {
    if [ ${#AGENT_NAMES[@]} -eq 0 ]; then
        echo "==> No agents discovered under agents/*/compose.yml"
    else
        echo "==> Agents discovered: ${AGENT_NAMES[*]}"
    fi
    echo "==> AGENT_RUNTIMES=${AGENT_RUNTIMES}"
    echo "==> COMPOSE_FILE=${COMPOSE_FILE}"
}

cmd="${1:-up}"
shift || true

case "$cmd" in
    up)
        build_base
        print_plan
        docker compose up --build "$@"
        ;;
    down)
        docker compose down "$@"
        ;;
    logs)
        docker compose logs -f "$@"
        ;;
    ps)
        docker compose ps "$@"
        ;;
    rebuild)
        docker build --no-cache -f agents/base.Dockerfile -t "$BASE_IMAGE" .
        docker compose build --no-cache "$@"
        ;;
    *)
        # Transparent passthrough for anything else (exec, restart, etc.)
        docker compose "$cmd" "$@"
        ;;
esac
