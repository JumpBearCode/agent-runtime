"""Web UI launcher — argparse + uvicorn."""

import argparse
from pathlib import Path

from agent_runtime import config


def main():
    parser = argparse.ArgumentParser(description="Agent Frontend Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    parser.add_argument("--workspace", "-w", default=".", help="Workspace directory")
    parser.add_argument("--thinking", "-t", action="store_true", default=False, help="Enable extended thinking")
    parser.add_argument("--thinking-budget", type=int, default=10000, help="Max tokens for thinking")
    parser.add_argument("--settings", default=None, help="Path to settings folder (overrides .agent_settings)")
    parser.add_argument("--confirm", action="store_true", default=False, help="Enable confirmation for dangerous tools")
    args = parser.parse_args()

    # Set config directly — same process, no need for os.environ
    ws = Path(args.workspace).resolve() if args.workspace != "." else Path.cwd()
    if not ws.exists():
        ws.mkdir(parents=True)
    config.WORKDIR = ws
    config.THINKING_ENABLED = args.thinking
    config.THINKING_BUDGET = args.thinking_budget
    config.SETTINGS_OVERRIDE = args.settings
    config.CONFIRM = args.confirm

    import uvicorn
    uvicorn.run("agent_frontend.web.server:app",
                host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
