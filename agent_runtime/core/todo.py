"""Todo — in-memory structured todo list bound per-chat."""

VALID_STATUSES = {"pending", "in_progress", "completed"}


class Todo:
    def __init__(self):
        self._items: list[dict] = []

    def write(self, items: list[dict]) -> str:
        """Replace the entire todo list. Each item: {id, content, status}."""
        validated = []
        in_progress_count = 0
        for item in items:
            status = item.get("status", "pending")
            if status not in VALID_STATUSES:
                raise ValueError(f"Invalid status: {status}. Must be one of {VALID_STATUSES}")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({
                "id": item["id"],
                "content": item["content"],
                "status": status,
            })
        if in_progress_count > 1:
            raise ValueError("Only one item can be in_progress at a time.")
        self._items = validated
        return self.render()

    def read(self) -> str:
        """Return the rendered todo list."""
        return self.render() or "No todos."

    def render(self) -> str:
        if not self._items:
            return ""
        markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = []
        for item in self._items:
            marker = markers[item["status"]]
            lines.append(f"{marker} {item['id']}. {item['content']}")
        return "\n".join(lines)

    @property
    def has_content(self) -> bool:
        return bool(self._items)
