"""CLI entrypoint — argparse + REPL."""

import argparse

from . import config
from .sandbox import setup_workspace
from .tasks import TaskManager
from .skills import SkillLoader
from .background import BackgroundManager
from .compression import auto_compact
from .loop import agent_loop, build_system_prompt
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
    args = parser.parse_args()

    # Initialize workspace + sandbox
    setup_workspace(args.workspace)

    # Initialize managers
    tasks = TaskManager(config.WORKDIR / ".tasks")
    skill_loader = SkillLoader(config.WORKDIR / "skills")
    bg = BackgroundManager()

    # Wire managers into tools module (resolved at call time via lambdas)
    tools_mod.TASKS = tasks
    tools_mod.SKILL_LOADER = skill_loader
    tools_mod.BG = bg

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
    print("=" * 60)
    print("Multi-line input: end first line with \\ then blank line to submit.")
    print("Commands: /compact /tasks  |  quit/exit to leave.\n")

    history = []
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


if __name__ == "__main__":
    main()
