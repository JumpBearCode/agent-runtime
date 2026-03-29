"""CLI entrypoint — argparse + REPL."""

import argparse
from pathlib import Path

from . import config
from .sandbox import setup_workspace
from .tasks import TaskManager
from .skills import SkillLoader
from .background import BackgroundManager
from .compression import auto_compact
from .loop import agent_loop, build_system_prompt
from .mcp_client import MCPManager
from . import tools as tools_mod


def read_input() -> str:
    first = input("\033[36magent >> \033[0m")
    if not first.endswith("\\"):
        return first
    lines = [first[:-1]]
    while True:
        try:
            line = input("\033[36m   ... \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if line == "":
            break
        if line.endswith("\\"):
            lines.append(line[:-1])
        else:
            lines.append(line)
            break
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Agent runtime")
    parser.add_argument(
        "--workspace", "-w", default=None,
        help="Workspace directory. '.' for cwd. Enables Docker sandbox if available. "
             "If omitted, runs without sandbox.",
    )
    parser.add_argument(
        "--thinking", "-t", action="store_true", default=False,
        help="Enable extended thinking (forces temperature=1).",
    )
    parser.add_argument(
        "--thinking-budget", type=int, default=10000,
        help="Max tokens for thinking per turn (default: 10000).",
    )
    parser.add_argument(
        "--mcp-config", default=None,
        help="Path to MCP config JSON file (default: looks for mcp.json in workspace).",
    )
    args = parser.parse_args()

    # Initialize workspace + sandbox
    setup_workspace(args.workspace)

    # Thinking config
    config.THINKING_ENABLED = args.thinking
    config.THINKING_BUDGET = args.thinking_budget

    # Initialize managers
    tasks = TaskManager(config.WORKDIR / ".tasks")
    skill_loader = SkillLoader(config.WORKDIR / "skills")
    bg = BackgroundManager()
    mcp = MCPManager()

    # Wire managers into tools module
    tools_mod.TASKS = tasks
    tools_mod.SKILL_LOADER = skill_loader
    tools_mod.BG = bg
    tools_mod.MCP = mcp

    # MCP: load config and connect to servers
    mcp_config_path = Path(args.mcp_config) if args.mcp_config else config.WORKDIR / "mcp.json"
    mcp_config = mcp.load_config(mcp_config_path)
    if mcp_config.get("servers"):
        print("  MCP servers:")
        mcp.start(mcp_config)
        tools_mod.rebuild_tools()

    system = build_system_prompt(skill_loader)

    # Startup info
    print("=" * 60)
    print(f"  Workspace: {config.WORKDIR}")
    if config.SANDBOX_ENABLED:
        print(f"  Sandbox:   Docker ({config.CONTAINER_NAME})")
    else:
        if args.workspace is None:
            print("  \033[33mSandbox:   OFF — bash can escape safe_path.\033[0m")
            print("  \033[33m           Use --workspace <dir> to enable Docker sandbox.\033[0m")
        else:
            print("  \033[33mSandbox:   OFF (Docker not available)\033[0m")
    print(f"  Model:     {config.MODEL}")
    if config.THINKING_ENABLED:
        print(f"  Thinking:  ON (budget: {config.THINKING_BUDGET} tokens)")
    if mcp.tool_names:
        print(f"  MCP tools: {len(mcp.tool_names)} from {len(mcp._servers)} server(s)")
    print("=" * 60)
    print("Multi-line input: end first line with \\ then blank line to submit.")
    print("Commands: /compact /tasks  |  quit/exit to leave.\n")

    history = []
    try:
        while True:
            try:
                query = read_input()
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            if query.strip() == "/compact":
                history[:] = auto_compact(history)
                print("[compacted]\n")
                continue
            if query.strip() == "/tasks":
                print(tasks.list_all())
                print()
                continue
            history.append({"role": "user", "content": query})
            try:
                agent_loop(history, system, bg)
            except KeyboardInterrupt:
                print("\n[interrupted]")
                if history and history[-1]["role"] == "assistant":
                    history.pop()
                continue
            except Exception as e:
                print(f"\n[error: {e}]")
                if history and history[-1]["role"] == "assistant":
                    history.pop()
                continue
            response_content = history[-1]["content"]
            if isinstance(response_content, list):
                for block in response_content:
                    if hasattr(block, "text"):
                        print(block.text)
            print()
    finally:
        mcp.shutdown()


if __name__ == "__main__":
    main()
