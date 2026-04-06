"""Rich CLI entry point — argparse, REPL, event consumer."""

import argparse
import asyncio
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from agent_frontend.engine import AgentEngine, EngineConfig
from .display import StreamState, create_streaming_display, display_final
from .formatters import format_session_table


console = Console()


def _build_banner(info: dict, session_id: str) -> Panel:
    lines = []
    lines.append(f"Workspace: {info['workspace']}")
    lines.append(f"Session:   {session_id}")
    lines.append(f"Model:     {info['model']}")
    if info.get("thinking"):
        lines.append(f"Thinking:  ON (budget: {info['thinking_budget']} tokens)")
    if info.get("mcp_tool_count"):
        lines.append(f"MCP tools: {info['mcp_tool_count']} from {info['mcp_server_count']} server(s)")
    return Panel("\n".join(lines), title="agent-cli", border_style="cyan")


async def _stream_turn(engine: AgentEngine, session_id: str, query: str):
    state = StreamState()
    try:
        with Live(console=console, refresh_per_second=12, transient=True) as live:
            async for event in engine.chat_stream(session_id, query):
                if event.type == "confirm_request":
                    # Pause Live display, ask user, resume
                    live.stop()
                    preview = event.preview or ""
                    try:
                        resp = input(f"  \033[35m? Allow {event.tool_name}{preview}? [Y/n]\033[0m ")
                    except (EOFError, KeyboardInterrupt):
                        resp = "n"
                    engine.respond_confirm(resp.strip().lower() in ("", "y", "yes"))
                    live.start()
                    continue
                state.handle_event(event)
                live.update(create_streaming_display(state))
    except KeyboardInterrupt:
        console.print("[interrupted]", style="yellow")
        return
    display_final(state, console)


def main():
    parser = argparse.ArgumentParser(description="Agent Rich CLI")
    parser.add_argument("--workspace", "-w", default=None, help="Workspace directory")
    parser.add_argument("--thinking", "-t", action="store_true", default=False, help="Enable extended thinking")
    parser.add_argument("--thinking-budget", type=int, default=10000, help="Max tokens for thinking")
    parser.add_argument("--settings", default=None, help="Path to settings folder (overrides .agent_settings)")
    parser.add_argument("--session", "-s", default=None, help="Session ID to resume")
    parser.add_argument("--list-sessions", action="store_true", default=False, help="List sessions and exit")
    args = parser.parse_args()

    cfg = EngineConfig(
        workspace=args.workspace,
        thinking=args.thinking,
        thinking_budget=args.thinking_budget,
        settings=args.settings,
    )
    engine = AgentEngine(cfg)

    if args.list_sessions:
        sessions = engine.list_sessions()
        if sessions:
            console.print(format_session_table(sessions))
        else:
            console.print("No sessions found.", style="dim")
        engine.shutdown()
        return

    # Session setup
    if args.session:
        engine.load_session(args.session)
        session_id = args.session
    else:
        session_id = engine.create_session()

    console.print(_build_banner(engine.startup_info, session_id))
    skill_names = engine.get_skill_names()
    if skill_names:
        console.print(f"  Skills ({len(skill_names)}):", style="bold")
        for name, desc in skill_names.items():
            console.print(f"    [blue]/{name}[/blue]  {desc}")
        console.print()
    console.print("Multi-line input: end first line with \\ then blank line to submit.")
    console.print("Commands: /compact /todo /tools /skills /sessions  |  quit/exit to leave.\n")

    # Lazy import prompt_toolkit for REPL
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.formatted_text import HTML

    history_file = Path.home() / ".agent_frontend_history"
    session_prompt = PromptSession(
        history=FileHistory(str(history_file)),
        auto_suggest=AutoSuggestFromHistory(),
    )

    try:
        while True:
            try:
                line = session_prompt.prompt(HTML("<cyan>agent >> </cyan>"))
            except (EOFError, KeyboardInterrupt):
                break

            if not line.strip():
                continue

            # Multiline support (backslash continuation)
            if line.endswith("\\"):
                lines = [line[:-1]]
                while True:
                    try:
                        cont = session_prompt.prompt(HTML("<cyan>   ... </cyan>"))
                    except (EOFError, KeyboardInterrupt):
                        break
                    if cont == "":
                        break
                    if cont.endswith("\\"):
                        lines.append(cont[:-1])
                    else:
                        lines.append(cont)
                        break
                query = "\n".join(lines)
            else:
                query = line

            stripped = query.strip().lower()
            if stripped in ("exit", "quit", "q"):
                break

            if query.strip() == "/compact":
                engine.compact(session_id)
                console.print("[compacted]", style="dim")
                continue

            if query.strip() == "/todo":
                todo_text = engine.get_todo()
                console.print(todo_text or "(empty)")
                continue

            if query.strip() == "/tools":
                tools = engine.get_tools()
                builtin = [t for t in tools if not t.startswith("mcp_")]
                mcp = [t for t in tools if t.startswith("mcp_")]
                if builtin:
                    console.print("  Built-in:", style="bold")
                    for t in builtin:
                        console.print(f"    {t}")
                if mcp:
                    console.print("  MCP:", style="bold cyan")
                    for t in mcp:
                        badge = "[ADF] " if t.startswith("mcp_adf_") else ""
                        console.print(f"    {badge}{t}", style="cyan")
                console.print(f"\n  {len(tools)} tools available", style="dim")
                continue

            if query.strip() == "/skills":
                skills_desc = engine.get_skills()
                console.print(skills_desc or "(no skills)")
                continue

            if query.strip() == "/sessions":
                sessions = engine.list_sessions()
                if sessions:
                    console.print(format_session_table(sessions))
                else:
                    console.print("No sessions.", style="dim")
                continue

            # Inline skill expansion: scan for /skill-name anywhere in input
            for match in re.findall(r'/([A-Za-z0-9_-]+)', query):
                content = engine.get_skill_content(match)
                if content is not None:
                    query = query.replace(f"/{match}", content, 1)
                    console.print(f"  [blue]loaded skill: {match}[/blue]")

            try:
                asyncio.run(_stream_turn(engine, session_id, query))
            except KeyboardInterrupt:
                console.print("\n[interrupted]", style="yellow")
                continue
            except Exception as e:
                console.print(f"[error: {e}]", style="red")
                continue

            console.print()
    finally:
        if engine.tracker.turn_count > 0:
            console.print(f"\n{engine.tracker.format_total(engine.startup_info['model'])}", style="dim")
        engine.shutdown()


if __name__ == "__main__":
    main()
