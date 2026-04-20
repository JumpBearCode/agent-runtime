"""Context compression — micro_compact, auto_compact, should_compact."""

import json
import time

from . import config


def should_compact(tracker) -> bool:
    """Check if last turn's actual context size exceeds threshold.

    Uses real token counts from the API response instead of heuristics.
    Returns False on the first turn (no history yet) — context is always small then.
    """
    if not tracker or not tracker._turns:
        return False
    last = tracker._turns[-1]
    actual_context = last.input_tokens + last.cache_read_input_tokens + last.cache_creation_input_tokens
    return actual_context > config.COMPACT_THRESHOLD


def micro_compact(messages: list) -> None:
    """In-place compression: strip old thinking blocks and truncate old tool results."""
    # --- Strip old thinking blocks ---
    if config.THINKING_ENABLED:
        for msg in messages[:-1]:
            if msg["role"] == "assistant" and isinstance(msg.get("content"), list):
                msg["content"] = [
                    b for b in msg["content"]
                    if not (hasattr(b, "type") and b.type == "thinking")
                ]

    # --- Clear old tool results ---
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))
    if len(tool_results) <= config.KEEP_RECENT:
        return
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name
    to_clear = tool_results[:-config.KEEP_RECENT]
    for _, _, result in to_clear:
        if isinstance(result.get("content"), str) and len(result["content"]) > 100:
            tool_id = result.get("tool_use_id", "")
            tool_name = tool_name_map.get(tool_id, "unknown")
            result["content"] = f"[Previous: used {tool_name}]"


def auto_compact(messages: list, tracker=None) -> list:
    # Save full transcript to disk for recovery/debugging
    transcript_dir = config.WORKDIR / ".transcripts"
    transcript_dir.mkdir(exist_ok=True)
    transcript_path = transcript_dir / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[transcript saved: {transcript_path}]")

    # LLM summarizes — take the TAIL so recent context is preserved
    conversation_text = json.dumps(messages, default=str)[-80000:]
    response = config.client.messages.create(
        model=config.MODEL,
        messages=[{"role": "user", "content":
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details.\n\n" + conversation_text}],
        max_tokens=2000,
    )
    if tracker and response.usage:
        tracker.record(response.usage)
    summary = response.content[0].text
    return [
        {"role": "user", "content": f"[Conversation compressed]\n\n{summary}"},
        {"role": "assistant", "content": "Understood. Continuing from the summary above."},
    ]
