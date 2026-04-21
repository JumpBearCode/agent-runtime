"""Web UI launcher.

Reads configuration from environment. On startup we load `.env` from the
nearest ancestor directory (so the shared project-root .env works whether
you launch from the repo root or from agent_frontend/).

Environment:
    AGENT_RUNTIMES     comma-sep list of runtime base URLs (default localhost:8001)
    CHAT_STORAGE       'local' (default, SQLite) or 'postgres'
    CHAT_SQLITE_PATH   local SQLite file (default ./agent_frontend.db)
    CHAT_POSTGRES_URL  postgres conn string (required when CHAT_STORAGE=postgres)
    CHAT_USER_ID       single-tenant user id (default 'local')
    LOG_LEVEL          default INFO
"""

import argparse
import logging
import os

from dotenv import find_dotenv, load_dotenv


def main():
    parser = argparse.ArgumentParser(description="Agent Frontend web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    parser.add_argument(
        "--env-file",
        default=None,
        help="Override the .env file path (default: auto-discover upward from CWD).",
    )
    args = parser.parse_args()

    # Pull env from .env: explicit --env-file wins; otherwise walk up from CWD
    # (so the shared project-root .env is picked up regardless of launch dir).
    dotenv_path = args.env_file or find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    if dotenv_path:
        logging.getLogger(__name__).info("loaded env from %s", dotenv_path)

    import uvicorn

    uvicorn.run(
        "agent_frontend.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
