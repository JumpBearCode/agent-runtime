"""Agent loop — the core while loop that calls tools until the model stops."""

from . import config
from .tools import TOOLS, TOOL_HANDLERS
from .subagent import run_subagent
from .compression import micro_compact, auto_compact, estimate_tokens
from .background import BackgroundManager

TASK_TOOL_NAMES = {"task_create", "task_update", "task_list", "task_get"}


def _format_args(tool_name: str, args: dict) -> str:
    """One-line summary of tool args for display."""
    if tool_name == "bash":
        return f"  $ {args.get('command', '')[:120]}"
    if tool_name == "read_file":
        return f"  {args.get('path', '')}"
    if tool_name in ("write_file", "edit_file"):
        return f"  {args.get('path', '')}"
    if tool_name == "task_create":
        return f"  \"{args.get('subject', '')}\""
    if tool_name == "task_update":
        return f"  #{args.get('task_id', '')} → {args.get('status', '')}"
    if tool_name == "load_skill":
        return f"  {args.get('name', '')}"
    if tool_name == "background_run":
        return f"  $ {args.get('command', '')[:120]}"
    return ""


def build_system_prompt(skill_loader) -> str:
    skills = skill_loader.get_descriptions() if skill_loader else "(no skills available)"
    sandbox_note = " (sandboxed via Docker)" if config.SANDBOX_ENABLED else ""
    return f"""You are a coding agent at {config.WORKDIR}.{sandbox_note}
Use task tools to plan and track multi-step work. Mark in_progress before starting, completed when done.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.
Use subagent for isolated exploration. Use background_run for long-running commands.
All file operations (read_file, write_file, edit_file) are restricted to the workspace directory.
Prefer tools over prose.

Skills available:
{skills}"""


def agent_loop(messages: list, system: str, bg: BackgroundManager):
    rounds_since_task = 0

    while True:
        micro_compact(messages)
        if estimate_tokens(messages) > config.COMPACT_THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = auto_compact(messages)

        # Drain background notifications
        notifs = bg.drain_notifications()
        if notifs and messages:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            bg_block = f"\n<background-results>\n{notif_text}\n</background-results>"
            if messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], str):
                messages[-1]["content"] += bg_block
            else:
                messages.append({"role": "user", "content": bg_block})

        response = config.client.messages.create(
            model=config.MODEL, system=system, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        used_task_tool = False
        manual_compact = False

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "subagent":
                    desc = block.input.get("description", "subtask")
                    print(f"\033[33m> subagent\033[0m  ({desc}) {block.input['prompt'][:80]}")
                    try:
                        output = run_subagent(block.input["prompt"])
                    except Exception as e:
                        output = f"Error: subagent failed: {e}"
                elif block.name == "compact":
                    manual_compact = True
                    output = "Compressing..."
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"
                args_summary = _format_args(block.name, block.input)
                print(f"\033[33m> {block.name}\033[0m{args_summary}")
                result_preview = str(output).strip()[:300]
                if result_preview:
                    print(f"  {result_preview}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})

                if block.name in TASK_TOOL_NAMES:
                    used_task_tool = True

        rounds_since_task = 0 if used_task_tool else rounds_since_task + 1
        if rounds_since_task >= 3:
            results.append({"type": "text", "text": "<reminder>Update your tasks.</reminder>"})

        messages.append({"role": "user", "content": results})

        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages)
