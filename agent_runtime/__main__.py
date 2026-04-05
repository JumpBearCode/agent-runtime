"""CLI entrypoint — argparse + REPL."""

import argparse
import re
from pathlib import Path

from . import config
from .todo import Todo
from .skills import SkillLoader
from .compression import auto_compact
from .loop import agent_loop, build_system_prompt, _inject_todo
from .mcp_client import MCPManager
from .tracking import TokenTracker
from .hooks import HookManager, HumanConfirmHook, validate_hitl
from .session import SessionStore
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
        help="Workspace directory (default: cwd). Agent file operations are restricted to this path.",
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
        "--settings", default=None,
        help="Path to settings folder (overrides project and user-level .agent_settings).",
    )
    parser.add_argument(
        "--confirm", action="store_true", default=False,
        help="Enable human-in-the-loop confirmation for dangerous tool calls.",
    )
    parser.add_argument(
        "--session", "-s", default=None,
        help="Session ID to resume. If omitted, starts a new session.",
    )
    args = parser.parse_args()

    # Workspace
    if args.workspace:
        ws = Path(args.workspace).resolve() if args.workspace != "." else Path.cwd()
        if not ws.exists():
            ws.mkdir(parents=True)
            print(f"  Created workspace: {ws}")
        config.WORKDIR = ws

    # Config
    config.THINKING_ENABLED = args.thinking
    config.THINKING_BUDGET = args.thinking_budget
    config.SETTINGS_OVERRIDE = args.settings
    config.CONFIRM = args.confirm

    # Initialize managers
    todo = Todo()
    skill_loader = SkillLoader(config.WORKDIR / "skills")
    tracker = TokenTracker()
    mcp = MCPManager()
    hooks = HookManager()
    session = SessionStore()

    # Wire managers into tools module
    tools_mod.TODO = todo
    tools_mod.SKILL_LOADER = skill_loader
    tools_mod.MCP = mcp
    tools_mod.HOOKS = hooks

    # MCP: resolve from settings layers and connect
    mcp_cfg = config.resolve_mcp_config()
    if mcp_cfg.get("servers"):
        print("  MCP servers:")
        mcp.start(mcp_cfg)
        tools_mod.rebuild_tools()

    # Confirm hook — after MCP so TOOLS is fully populated for validation
    if config.CONFIRM:
        confirm_set = validate_hitl(config.resolve_hitl())
        hooks.add(HumanConfirmHook(confirm_tools=confirm_set))

    system = build_system_prompt(skill_loader, mcp_manager=mcp)

    # Session: resume or create new
    if args.session:
        history = session.load_session(args.session)
        print(f"  Session:   {session.session_id} (resumed, {len(history)} messages)")
    else:
        session.new_session()
        history = []

    # Startup info
    print("=" * 60)
    print(f"  Workspace: {config.WORKDIR}")
    print(f"  Session:   {session.session_id}")
    print(f"  Model:     {config.MODEL}")
    if config.THINKING_ENABLED:
        print(f"  Thinking:  ON (budget: {config.THINKING_BUDGET} tokens)")
    if mcp.tool_names:
        print(f"  MCP tools: {len(mcp.tool_names)} from {len(mcp._servers)} server(s)")
    if config.CONFIRM:
        print("  Confirm:   ON (dangerous tools require approval)")
    if skill_loader.skills:
        print(f"  Skills:    {len(skill_loader.skills)} available")
        for name in skill_loader.skills:
            desc = skill_loader.skills[name]["meta"].get("description", "")
            print(f"             \033[34m/{name}\033[0m  {desc}")
    print("=" * 60)
    print("Multi-line input: end first line with \\ then blank line to submit.")
    print("Commands: /compact /todo  |  quit/exit to leave.\n")

    try:
        while True:
            try:
                query = read_input()
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            if query.strip() == "/compact":
                history[:] = auto_compact(history, tracker)
                _inject_todo(history)
                print("[compacted]\n")
                continue
            if query.strip() == "/todo":
                print(todo.read())
                print()
                continue
            # Inline skill expansion: scan for /skill-name anywhere in input
            for match in re.findall(r'/([A-Za-z0-9_-]+)', query):
                if match in skill_loader.skills:
                    query = query.replace(f"/{match}", skill_loader.get_content(match), 1)
                    print(f"\033[34m  loaded skill: {match}\033[0m")
            history.append({"role": "user", "content": query})
            session.save_turn(history[-1])
            try:
                agent_loop(history, system, tracker, session=session)
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
            print(f"\033[2m{tracker.format_total(config.MODEL)}\033[0m")
            print()
    finally:
        if tracker.turn_count > 0:
            print(f"\n{tracker.format_total(config.MODEL)}")
        mcp.shutdown()


if __name__ == "__main__":
    main()
