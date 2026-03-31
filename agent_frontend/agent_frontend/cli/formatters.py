"""Formatting helpers for the Rich CLI."""

from rich.table import Table
from rich.text import Text


def format_tool_compact(name: str, args_summary: str) -> str:
    """Format tool call as compact string, e.g. 'Bash($ git status)'."""
    clean = args_summary.strip()
    if clean:
        return f"{name}({clean})"
    return name


def format_token_line(usage) -> str:
    """Format token usage as compact string, e.g. 'in:12,340 out:487 cached:8,200 $0.0234'."""
    if usage is None:
        return ""
    turn = usage.turn if hasattr(usage, "turn") else usage.get("turn", {})
    if not turn:
        return ""
    inp = turn.get("input", 0) + turn.get("cache_creation", 0) + turn.get("cache_read", 0)
    out = turn.get("output", 0)
    cached = turn.get("cache_read", 0)
    cost_str = usage.cost if hasattr(usage, "cost") else usage.get("cost", "")
    parts = [f"in:{inp:,}", f"out:{out:,}"]
    if cached:
        parts.append(f"cached:{cached:,}")
    if cost_str:
        parts.append(cost_str)
    return " ".join(parts)


def format_session_table(sessions: list[dict]) -> Table:
    """Build a Rich Table of sessions."""
    table = Table(title="Sessions", show_lines=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Created", style="dim")
    table.add_column("Messages", justify="right")
    table.add_column("Size", justify="right", style="dim")
    for s in sessions:
        table.add_row(
            s.get("id", "?"),
            s.get("created", "?"),
            str(s.get("messages", "?")),
            s.get("size", "?"),
        )
    return table
