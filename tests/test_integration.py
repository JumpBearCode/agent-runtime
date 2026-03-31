"""
Integration test: verify MCP + Skills wire up correctly through the engine.
Does NOT require Azure credentials — only tests discovery and loading.
"""
import pytest
from agent_frontend.engine import AgentEngine, EngineConfig


@pytest.fixture
def engine():
    """Engine with workspace at repo root (where mcp.json and skills/ are)."""
    e = AgentEngine(EngineConfig(workspace="."))
    yield e
    e.shutdown()


def test_mcp_tools_discovered(engine):
    """MCP tools should be discovered from mcp.json."""
    tools = engine.get_tools()
    mcp_tools = [t for t in tools if t.startswith("mcp_adf_")]
    # If ADF env vars are set, server connects and we get 5 tools.
    # If not set, MCP server may fail to start — that's OK, skip.
    if not mcp_tools:
        pytest.skip("ADF MCP server not available (missing env vars)")
    assert len(mcp_tools) == 5
    assert "mcp_adf_list_pipelines" in mcp_tools


def test_skills_loaded(engine):
    """Skills should be loaded from skills/ directory."""
    skills = engine.get_skills()
    assert "test-linked-service" in skills
    assert "find-pipelines-by-service" in skills
    assert "adf-overview" in skills


def test_system_prompt_includes_skills(engine):
    """System prompt should reference available skills."""
    assert "test-linked-service" in engine.system
    assert "adf-overview" in engine.system


def test_startup_info(engine):
    """Startup info should reflect MCP state."""
    info = engine.startup_info
    assert "mcp_tool_count" in info
    assert "workspace" in info
