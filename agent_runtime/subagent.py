"""Subagent — fresh-context child agent for isolated tasks."""

from . import config
from .tools import CHILD_TOOLS, TOOL_HANDLERS


def run_subagent(prompt: str) -> str:
    system = f"You are a coding subagent at {config.WORKDIR}. Complete the given task, then summarize your findings."
    sub_messages = [{"role": "user", "content": prompt}]
    for _ in range(30):
        response = config.client.messages.create(
            model=config.MODEL, system=system, messages=sub_messages,
            tools=CHILD_TOOLS, max_tokens=8000,
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        sub_messages.append({"role": "user", "content": results})
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"
