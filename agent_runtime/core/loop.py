"""Agent loop — streaming + optional thinking."""

import json
import sys
from pathlib import Path

from . import config
from . import tools as tools_mod
from .tools import TOOLS, dispatch_tool

from .compression import micro_compact, auto_compact, should_compact
from .hooks import AbortRound
from .tracking import TokenTracker

TODO_TOOL_NAMES = {"todo_write", "todo_read"}


def _format_args(tool_name: str, args: dict) -> str:
    """One-line summary of tool args for display."""
    if tool_name == "bash":
        return f"  $ {args.get('command', '')[:120]}"
    if tool_name == "read_file":
        return f"  {args.get('path', '')}"
    if tool_name in ("write_file", "edit_file"):
        return f"  {args.get('path', '')}"
    if tool_name == "todo_write":
        return f"  ({len(args.get('items', []))} items)"
    if tool_name == "todo_read":
        return ""
    if tool_name == "load_skill":
        return f"  {args.get('name', '')}"
    if tool_name.startswith("mcp_"):
        summary = json.dumps(args, ensure_ascii=False)[:120]
        return f"  {summary}"
    return ""


_DEFAULT_PROMPT_TEMPLATE = """You are a coding agent at {workdir}.
Use todo_write to plan multi-step work and track progress. Update the todo list as you complete steps. Todo state survives compaction.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.

All file operations (read_file, write_file, edit_file) are restricted to the workspace directory.
Prefer tools over prose."""


def build_system_prompt(skill_loader, mcp_manager=None) -> str:
    """Compose the system prompt from a base template + dynamic sections.

    Base template comes from config.SYSTEM_PROMPT_FILE if set and readable
    (this is how per-agent containers inject their identity), otherwise
    falls back to the generic coding-agent template. Skills and MCP tool
    inventories are always appended dynamically so the file template doesn't
    need to know which skills/MCP servers happened to load this run.
    """
    skills = skill_loader.get_descriptions() if skill_loader else "(no skills available)"
    workdir = str(config.WORKDIR)

    base = None
    if config.SYSTEM_PROMPT_FILE:
        path = Path(config.SYSTEM_PROMPT_FILE)
        if path.is_file():
            base = path.read_text().rstrip()
    if base is None:
        base = _DEFAULT_PROMPT_TEMPLATE.format(workdir=workdir)

    mcp_section = ""
    if mcp_manager and mcp_manager.tool_names:
        mcp_tools_list = ", ".join(sorted(mcp_manager.tool_names))
        mcp_section = f"""

MCP (Model Context Protocol) tools are available as NATIVE tool_use calls — call them exactly like bash, read_file, etc.
Do NOT run MCP tools via bash. They are tool_use functions, not shell commands.
ALWAYS prefer MCP tools over bash/curl for interacting with external services.
Available MCP tools: {mcp_tools_list}"""

    return f"{base}{mcp_section}\n\nSkills available:\n{skills}"


def _stream_response(system: str, messages: list, on_event=None):
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
                    if on_event:
                        on_event({"type": "thinking_start"})
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
                    if on_event:
                        on_event({"type": "text_delta", "text": delta.text})
                elif delta.type == "thinking_delta":
                    sys.stdout.write(delta.thinking)
                    sys.stdout.flush()
                    current_thinking += delta.thinking
                    if on_event:
                        on_event({"type": "thinking_delta", "text": delta.thinking})
                elif delta.type == "input_json_delta":
                    current_tool_input_json += delta.partial_json

            elif event.type == "content_block_stop":
                if current_block_type == "text" and current_text:
                    # Use the text block object from the final message
                    in_text = False
                    sys.stdout.write("\n")
                    if on_event:
                        on_event({"type": "text_stop"})
                elif current_block_type == "thinking":
                    in_thinking = False
                    sys.stdout.write("\033[0m\n")  # reset dim
                    if on_event:
                        on_event({"type": "thinking_stop"})
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

    if on_event:
        on_event({"type": "message_done", "stop_reason": stop_reason,
                  "usage": {"input": usage.input_tokens, "output": usage.output_tokens,
                            "cache_creation": getattr(usage, 'cache_creation_input_tokens', 0),
                            "cache_read": getattr(usage, 'cache_read_input_tokens', 0)}})

    return content_blocks, stop_reason, usage


def _inject_todo(messages: list):
    """Inject current todo state into the first user message (after compact).

    Merges into the existing first user message to preserve user/assistant
    alternation instead of inserting a separate user message at index 0.
    """
    todo = tools_mod.active_todo()
    if not (todo and todo.has_content):
        return
    todo_block = f"<todo>\n{todo.read()}\n</todo>\n\n"
    # After auto_compact, messages[0] is always a user message — prepend todo to it.
    if messages and messages[0]["role"] == "user" and isinstance(messages[0]["content"], str):
        messages[0]["content"] = todo_block + messages[0]["content"]
    else:
        # Fallback: shouldn't happen after auto_compact, but be safe.
        messages.insert(0, {"role": "user", "content": todo_block})


def agent_loop(messages: list, system: str, tracker: TokenTracker = None, on_event=None):
    rounds_since_todo = 0

    while True:
        micro_compact(messages)
        if should_compact(tracker):
            print("[auto_compact triggered]")
            if on_event:
                on_event({"type": "status", "message": "auto_compact triggered"})
            messages[:] = auto_compact(messages, tracker)
            _inject_todo(messages)

        # Stream the response (text/thinking printed live)
        content_blocks, stop_reason, usage = _stream_response(system, messages, on_event=on_event)

        # Track token usage. Build the event payload now but defer emission to
        # end-of-round (after tool execution) so the frontend can render it
        # after that round's tool blocks instead of before them. CLI terminal
        # print stays at its original position to preserve existing CLI output.
        token_event = None
        if tracker and usage:
            turn = tracker.record(usage)
            print(f"\033[2m  [{tracker.format_turn(turn, config.MODEL)}]\033[0m")
            if on_event:
                token_event = {"type": "token_usage",
                               "turn": {"input": turn.input_tokens,
                                        "output": turn.output_tokens,
                                        "cache_creation": turn.cache_creation_input_tokens,
                                        "cache_read": turn.cache_read_input_tokens,
                                        "cost": turn.cost(config.MODEL)},
                               "total": {"input": tracker.total.input_tokens,
                                         "output": tracker.total.output_tokens,
                                         "cache_creation": tracker.total.cache_creation_input_tokens,
                                         "cache_read": tracker.total.cache_read_input_tokens,
                                         "cost": tracker.total.cost(config.MODEL)},
                               "cost": tracker.format_turn(turn, config.MODEL)}

        # Append full assistant message (including thinking blocks for context)
        messages.append({"role": "assistant", "content": content_blocks})

        if stop_reason != "tool_use":
            if on_event:
                if token_event:
                    on_event(token_event)
                on_event({"type": "done", "stop_reason": stop_reason})
            return

        # Execute tools
        results = []
        used_todo_tool = False
        manual_compact = False
        aborted_reason: str | None = None

        for idx, block in enumerate(content_blocks):
            if block.type != "tool_use":
                continue

            # If a prior tool aborted the round, backfill placeholders for the
            # rest so the assistant's tool_use blocks all have matching
            # tool_results — Anthropic API requires this pairing.
            if aborted_reason is not None:
                placeholder = f"Blocked: round aborted ({aborted_reason})."
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": placeholder})
                continue

            if on_event:
                on_event({"type": "tool_call", "id": block.id, "name": block.name,
                          "args": block.input, "args_summary": _format_args(block.name, block.input)})
            if block.name == "compact":
                manual_compact = True
                output = "Compressing..."
            else:
                try:
                    output = dispatch_tool(block.name, block.input)
                except AbortRound as e:
                    aborted_reason = e.reason or "hook requested abort"
                    output = f"Blocked: {aborted_reason}. Round ended; user may resend."
                except Exception as e:
                    output = f"Error: {e}"
            args_summary = _format_args(block.name, block.input)
            print(f"\033[33m> {block.name}\033[0m{args_summary}")
            result_preview = str(output).strip()[:300]
            if result_preview:
                print(f"  {result_preview}")
            output_str = str(output)
            orig_len = len(output_str)
            if orig_len > config.TOOL_OUTPUT_LIMIT:
                output_str = (output_str[:config.TOOL_OUTPUT_LIMIT]
                              + f"\n...[truncated, {orig_len - config.TOOL_OUTPUT_LIMIT} more chars]")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output_str})
            if on_event:
                on_event({"type": "tool_result", "id": block.id, "name": block.name,
                          "output": output_str, "is_error": output_str.startswith("Error:")})

            if block.name in TODO_TOOL_NAMES:
                used_todo_tool = True

        # End-of-round: emit token_usage after all tool results have been sent
        # so the frontend renders it below the round's tool blocks.
        if on_event and token_event:
            on_event(token_event)

        rounds_since_todo = 0 if used_todo_tool else rounds_since_todo + 1
        todo = tools_mod.active_todo()
        if rounds_since_todo >= 5 and todo and todo.has_content:
            results.append({"type": "text", "text": f"<todo>\n{todo.read()}\n</todo>"})

        messages.append({"role": "user", "content": results})

        if aborted_reason is not None:
            print(f"[round aborted: {aborted_reason}]")
            if on_event:
                on_event({"type": "done", "stop_reason": "hitl_timeout"})
            return

        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages, tracker)
            _inject_todo(messages)
