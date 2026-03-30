"""Agent loop — streaming + optional thinking."""

import json
import sys

from . import config
from .tools import TOOLS, dispatch_tool
from .subagent import run_subagent
from .compression import micro_compact, auto_compact, estimate_tokens
from .background import BackgroundManager
from .tracking import TokenTracker

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
    if tool_name.startswith("mcp_"):
        summary = json.dumps(args, ensure_ascii=False)[:120]
        return f"  {summary}"
    return ""


def build_system_prompt(skill_loader, mcp_manager=None) -> str:
    skills = skill_loader.get_descriptions() if skill_loader else "(no skills available)"
    if config.SANDBOX_ENABLED:
        sandbox_note = " (sandboxed via Docker)"
        workdir = "/workspace"
        workspace_hint = (
            "/workspace is the project root directory (mounted from the host). "
            "Initialize and create all project files directly under /workspace — "
            "do NOT create a nested subdirectory with the project name. "
            "Use /workspace as your working directory for all operations."
        )
    else:
        sandbox_note = ""
        workdir = str(config.WORKDIR)
        workspace_hint = ""

    mcp_section = ""
    if mcp_manager and mcp_manager.tool_names:
        mcp_tools_list = ", ".join(sorted(mcp_manager.tool_names))
        mcp_section = f"""
MCP (Model Context Protocol) tools are available. ALWAYS prefer MCP tools over bash/curl for interacting with external services.
For example, use mcp_github_* tools for ANY GitHub operations instead of curl/gh/git commands.
Available MCP tools: {mcp_tools_list}
"""

    return f"""You are a coding agent at {workdir}.{sandbox_note}
{workspace_hint}
Use task tools to plan and track multi-step work. Mark in_progress before starting, completed when done.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.
Use subagent for isolated exploration. Use background_run for long-running commands.
All file operations (read_file, write_file, edit_file) are restricted to the workspace directory.
Prefer tools over prose.
{mcp_section}
Skills available:
{skills}"""


def _stream_response(system: str, messages: list):
    """Stream API call, print text/thinking live, return (content_blocks, stop_reason, usage)."""
    # -- Prompt caching: mark system, tools tail, and messages tail --
    # System prompt as cacheable block
    cached_system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    # Cache breakpoint on last tool schema
    cached_tools = [dict(t) for t in TOOLS]  # shallow copy to avoid mutating originals
    if cached_tools:
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

    # Cache breakpoint on the last user message (so next turn hits cache on entire prefix)
    # Max 4 cache_control blocks allowed by API: system(1) + tools(1) + messages(max 2)
    # Strip old cache_control from all messages, then add to last user message only
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)

    if messages:
        last = messages[-1]
        if last["role"] == "user":
            content = last["content"]
            if isinstance(content, str):
                messages[-1] = {**last, "content": [
                    {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                ]}
            elif isinstance(content, list) and content:
                last_block = content[-1]
                if isinstance(last_block, dict):
                    last_block["cache_control"] = {"type": "ephemeral"}

    max_tokens = 16000
    if config.THINKING_ENABLED:
        max_tokens = max(max_tokens, config.THINKING_BUDGET + 8000)

    kwargs = dict(
        model=config.MODEL, system=cached_system, messages=messages,
        tools=cached_tools, max_tokens=max_tokens,
    )
    if config.THINKING_ENABLED:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": config.THINKING_BUDGET}
        kwargs["temperature"] = 1

    content_blocks = []
    stop_reason = None

    # Track current block being streamed
    current_block_type = None
    current_text = ""
    current_thinking = ""
    current_tool_name = ""
    current_tool_input_json = ""
    current_tool_id = ""
    in_text = False
    in_thinking = False

    with config.client.messages.stream(**kwargs) as stream:
        for event in stream:
            # --- Block lifecycle ---
            if event.type == "content_block_start":
                block = event.content_block
                current_block_type = block.type
                if block.type == "text":
                    in_text = True
                    current_text = ""
                elif block.type == "thinking":
                    in_thinking = True
                    current_thinking = ""
                    sys.stdout.write("\033[2m")  # dim for thinking
                elif block.type == "tool_use":
                    current_tool_name = block.name
                    current_tool_id = block.id
                    current_tool_input_json = ""

            elif event.type == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    sys.stdout.write(delta.text)
                    sys.stdout.flush()
                    current_text += delta.text
                elif delta.type == "thinking_delta":
                    sys.stdout.write(delta.thinking)
                    sys.stdout.flush()
                    current_thinking += delta.thinking
                elif delta.type == "input_json_delta":
                    current_tool_input_json += delta.partial_json

            elif event.type == "content_block_stop":
                if current_block_type == "text" and current_text:
                    # Use the text block object from the final message
                    in_text = False
                    sys.stdout.write("\n")
                elif current_block_type == "thinking":
                    in_thinking = False
                    sys.stdout.write("\033[0m\n")  # reset dim
                elif current_block_type == "tool_use":
                    # Parse accumulated JSON
                    try:
                        tool_input = json.loads(current_tool_input_json) if current_tool_input_json else {}
                    except json.JSONDecodeError:
                        tool_input = {}
                current_block_type = None

            elif event.type == "message_delta":
                stop_reason = event.delta.stop_reason

        # Get the final message with properly constructed content blocks
        final_message = stream.get_final_message()
        content_blocks = final_message.content
        usage = final_message.usage

    return content_blocks, stop_reason, usage


def agent_loop(messages: list, system: str, bg: BackgroundManager, tracker: TokenTracker = None):
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

        # Stream the response (text/thinking printed live)
        content_blocks, stop_reason, usage = _stream_response(system, messages)

        # Track token usage
        if tracker and usage:
            turn = tracker.record(usage)
            print(f"\033[2m  [{tracker.format_turn(turn, config.MODEL)}]\033[0m")

        # Append full assistant message (including thinking blocks for context)
        messages.append({"role": "assistant", "content": content_blocks})

        if stop_reason != "tool_use":
            return

        # Execute tools
        results = []
        used_task_tool = False
        manual_compact = False

        for block in content_blocks:
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
                    try:
                        output = dispatch_tool(block.name, block.input)
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
