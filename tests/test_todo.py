"""Todo: per-chat structured todo list."""

import pytest

from agent_runtime.core.todo import Todo, VALID_STATUSES


def test_empty_todo_has_no_content():
    t = Todo()
    assert not t.has_content
    assert t.read() == "No todos."


def test_write_replaces_entire_list():
    t = Todo()
    t.write([{"id": 1, "content": "a", "status": "pending"}])
    t.write([{"id": 2, "content": "b", "status": "completed"}])
    rendered = t.read()
    assert "a" not in rendered
    assert "b" in rendered


def test_render_uses_status_markers():
    t = Todo()
    t.write([
        {"id": 1, "content": "a", "status": "pending"},
        {"id": 2, "content": "b", "status": "in_progress"},
        {"id": 3, "content": "c", "status": "completed"},
    ])
    rendered = t.read()
    assert "[ ] 1. a" in rendered
    assert "[>] 2. b" in rendered
    assert "[x] 3. c" in rendered


def test_invalid_status_rejected():
    t = Todo()
    with pytest.raises(ValueError, match="Invalid status"):
        t.write([{"id": 1, "content": "x", "status": "bogus"}])


def test_only_one_in_progress_allowed():
    t = Todo()
    with pytest.raises(ValueError, match="Only one item can be in_progress"):
        t.write([
            {"id": 1, "content": "a", "status": "in_progress"},
            {"id": 2, "content": "b", "status": "in_progress"},
        ])


def test_valid_statuses_constant():
    assert VALID_STATUSES == {"pending", "in_progress", "completed"}
