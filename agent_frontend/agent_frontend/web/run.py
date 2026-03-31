"""Web UI launcher — argparse + uvicorn."""

import argparse
import os


def main():
    parser = argparse.ArgumentParser(description="Agent Frontend Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    parser.add_argument("--workspace", "-w", default=".", help="Workspace directory")
    parser.add_argument("--thinking", "-t", action="store_true", default=False, help="Enable extended thinking")
    parser.add_argument("--thinking-budget", type=int, default=10000, help="Max tokens for thinking")
    parser.add_argument("--mcp-config", default=None, help="Path to MCP config JSON")
    parser.add_argument("--confirm", action="store_true", default=False, help="Enable confirmation for dangerous tools")
    args = parser.parse_args()

    os.environ["AGENT_WORKSPACE"] = args.workspace or "."
    if args.thinking:
        os.environ["AGENT_THINKING"] = "1"
        os.environ["AGENT_THINKING_BUDGET"] = str(args.thinking_budget)
    if args.mcp_config:
        os.environ["AGENT_MCP_CONFIG"] = args.mcp_config
    if args.confirm:
        os.environ["AGENT_CONFIRM"] = "1"

    import uvicorn
    uvicorn.run("agent_frontend.web.server:app",
                host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
