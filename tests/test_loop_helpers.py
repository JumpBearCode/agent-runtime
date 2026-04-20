"""loop.py helpers: _format_args + build_system_prompt (no LLM calls)."""

import pytest

from agent_runtime.core import config
from agent_runtime.core.loop import _format_args, build_system_prompt


# ── _format_args ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,args,expected_in", [
    ("bash",       {"command": "ls -la"},                   "ls -la"),
    ("read_file",  {"path": "x/y.txt"},                     "x/y.txt"),
    ("write_file", {"path": "out.md", "content": "..."},    "out.md"),
    ("edit_file",  {"path": "z.py"},                        "z.py"),
    ("load_skill", {"name": "alpha"},                       "alpha"),
    ("mcp_foo",    {"key": "value"},                        '"key"'),
])
def test_format_args(name, args, expected_in):
    assert expected_in in _format_args(name, args)


def test_format_args_todo_write_shows_count():
    out = _format_args("todo_write", {"items": [1, 2, 3]})
    assert "3" in out


def test_format_args_todo_read_empty():
    assert _format_args("todo_read", {}) == ""


def test_format_args_unknown_tool_returns_empty():
    assert _format_args("not_a_known_tool", {"x": 1}) == ""


def test_format_args_truncates_long_bash():
    long = "x" * 500
    out = _format_args("bash", {"command": long})
    # 120 char cap + "  $ " prefix
    assert len(out) < 200


# ── build_system_prompt ────────────────────────────────────────────────────

class _StubSkillLoader:
    def __init__(self, descriptions):
        self._d = descriptions

    def get_descriptions(self):
        return self._d


class _StubMCP:
    def __init__(self, tool_names):
        self.tool_names = set(tool_names)


def test_build_system_prompt_default_template_contains_workdir(monkeypatch):
    monkeypatch.setattr(config, "SYSTEM_PROMPT_FILE", None)
    monkeypatch.setattr(config, "WORKDIR", "/some/dir")
    out = build_system_prompt(skill_loader=_StubSkillLoader("(none)"), mcp_manager=None)
    assert "/some/dir" in out
    assert "Skills available" in out


def test_build_system_prompt_appends_skills_section(monkeypatch):
    monkeypatch.setattr(config, "SYSTEM_PROMPT_FILE", None)
    out = build_system_prompt(_StubSkillLoader("  - a: A\n  - b: B"))
    assert "  - a: A" in out
    assert "  - b: B" in out


def test_build_system_prompt_appends_mcp_section_when_tools_present(monkeypatch):
    monkeypatch.setattr(config, "SYSTEM_PROMPT_FILE", None)
    out = build_system_prompt(_StubSkillLoader("(none)"),
                              mcp_manager=_StubMCP(["mcp_x_one", "mcp_x_two"]))
    assert "mcp_x_one" in out
    assert "mcp_x_two" in out
    assert "MCP" in out


def test_build_system_prompt_omits_mcp_section_when_no_tools(monkeypatch):
    monkeypatch.setattr(config, "SYSTEM_PROMPT_FILE", None)
    out = build_system_prompt(_StubSkillLoader("(none)"),
                              mcp_manager=_StubMCP([]))
    assert "MCP (Model Context Protocol)" not in out


def test_build_system_prompt_uses_file_when_set(tmp_path, monkeypatch):
    f = tmp_path / "agent.md"
    f.write_text("CUSTOM AGENT IDENTITY")
    monkeypatch.setattr(config, "SYSTEM_PROMPT_FILE", str(f))
    out = build_system_prompt(_StubSkillLoader("(none)"))
    assert "CUSTOM AGENT IDENTITY" in out
    # Skills section still appended dynamically.
    assert "Skills available" in out


def test_build_system_prompt_falls_back_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SYSTEM_PROMPT_FILE", str(tmp_path / "does-not-exist.md"))
    monkeypatch.setattr(config, "WORKDIR", "/wd")
    out = build_system_prompt(_StubSkillLoader("(none)"))
    assert "/wd" in out  # default template's workdir injection
