# Prompt 4: ADF MCP + Skills Integration

## Task
Integrate ADF MCP Server and Skills with both frontends

## Context
We now have:
- `agent_frontend/engine.py` — shared engine wrapping agent_runtime
- `agent_frontend/cli/` — Rich CLI frontend
- `agent_frontend/web/` — Web frontend with SSE

The agent_runtime already supports MCP tools (via mcp_client.py) and skills (via skills.py).
We need to wire up the existing `adf_mcp_server.py` and create skills that work through
the engine, usable from both frontends.

## Reference Files (READ ALL BEFORE STARTING)
- `/Users/wqeq/Desktop/project/agent-runtime/adf_mcp_server.py` — ADF MCP server (uses FastMCP)
- `/Users/wqeq/Desktop/project/agent-runtime/ADFAgent/.claude/skills/test-linked-service/SKILL.md`
- `/Users/wqeq/Desktop/project/agent-runtime/ADFAgent/.claude/skills/find-pipelines-by-service/SKILL.md`
- `/Users/wqeq/Desktop/project/agent-runtime/agent_runtime/mcp_client.py` — how MCP connects
- `/Users/wqeq/Desktop/project/agent-runtime/agent_runtime/skills.py` — how skills load (reads {WORKDIR}/skills/)
- `/Users/wqeq/Desktop/project/agent-runtime/agent_runtime/tools.py` — dispatch_tool routes mcp_ prefixed tools

## Step 1: MCP Configuration

Create `mcp.json` at repo root:
```json
{
  "servers": {
    "adf": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "python", "adf_mcp_server.py"],
      "env": {}
    }
  }
}
```

Note: The ADF MCP server reads credentials from env vars (ADF_SUBSCRIPTION_ID, ADF_RESOURCE_GROUP,
ADF_FACTORY_NAME) which are loaded via python-dotenv from .env. The `env` field in mcp.json
can be empty — the server inherits the parent process environment.

Verify: `agent_runtime/mcp_client.py` loads this via `MCPManager.load_config()` and connects
to the server via stdio. The tools will be prefixed as: `mcp_adf_list_pipelines`,
`mcp_adf_get_pipeline`, `mcp_adf_list_data_flows`, `mcp_adf_get_data_flow`,
`mcp_adf_list_linked_services`.

## Step 2: Create Skills

agent_runtime's SkillLoader reads from `{WORKDIR}/skills/`. Create a `skills/` directory
at repo root with adapted versions of the ADFAgent skills.

### skills/test-linked-service/SKILL.md

Read the original at `ADFAgent/.claude/skills/test-linked-service/SKILL.md` and adapt:
- Keep the same YAML frontmatter format (`name`, `description`)
- In the body: replace ADFAgent-specific tool names with MCP tool names:
  - `adf_linked_service_list` → `mcp_adf_list_linked_services`
  - `adf_linked_service_get` → (not available via MCP — use list and filter)
  - `adf_linked_service_test` → (not available — note this limitation)
  - `adf_pipeline_list` → `mcp_adf_list_pipelines`
  - `adf_pipeline_get` → `mcp_adf_get_pipeline`
- Adjust the workflow to work with the available MCP tools
- Remove any references to `resolve_adf_target` (not applicable — single target via env)

### skills/find-pipelines-by-service/SKILL.md

Read the original at `ADFAgent/.claude/skills/find-pipelines-by-service/SKILL.md` and adapt:
- Same frontmatter format
- Replace tool references with MCP versions
- The workflow should:
  1. List linked services via `mcp_adf_list_linked_services`
  2. Filter by type (user provides type like "Snowflake", "AzureBlobStorage")
  3. List all pipelines via `mcp_adf_list_pipelines`
  4. For each pipeline, get definition via `mcp_adf_get_pipeline`
  5. Cross-reference activities to find which pipelines reference those linked services
  6. Also check data flows: `mcp_adf_list_data_flows`, `mcp_adf_get_data_flow`

### skills/adf-overview/SKILL.md (new)

```yaml
---
name: adf-overview
description: Get a comprehensive overview of the Azure Data Factory — pipelines, data flows, linked services counts and summaries
---
```

Body: Guide the agent to:
1. Call `mcp_adf_list_pipelines` → count and list names
2. Call `mcp_adf_list_data_flows` → count and list names
3. Call `mcp_adf_list_linked_services` → count, group by type
4. Present a summary table

## Step 3: Frontend Enhancements

### CLI: Add MCP/Skills info to startup banner and commands

Update `agent_frontend/cli/app.py`:
- Startup banner: if engine.startup_info["mcp_tool_count"] > 0, show:
  `MCP tools: {count} from {server_count} server(s)`
- `/tools` command: group tools into "Built-in" and "MCP" sections
  (MCP tools start with `mcp_`)
- `/skills` command: list available skills from `engine.get_skills()`
- When displaying tool calls in streaming, if tool name starts with `mcp_adf_`:
  prefix with `[ADF]` badge in blue

### Web: Add tools/skills info

Update `agent_frontend/web/`:
- `GET /api/tools` already exists — verify it returns MCP tools
- `GET /api/skills` already exists — verify it returns skill list
- In `script.js`: when rendering tool_call blocks, if `name.startsWith('mcp_adf_')`,
  add CSS class `tool-adf` for Azure-styled blue accent:
  ```css
  .tool-call-block.tool-adf {
      border-left: 3px solid #0078d4;  /* Azure blue */
  }
  .tool-call-block.tool-adf .tool-name::before {
      content: "ADF ";
      color: #0078d4;
      font-weight: 700;
  }
  ```

## Step 4: Smoke Test

Create `tests/test_integration.py`:

```python
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
    # get_skills returns a string of descriptions, check it contains our skill names
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
```

## Verification Steps
- [ ] `mcp.json` exists at repo root with correct ADF server config
- [ ] `skills/test-linked-service/SKILL.md` exists with valid frontmatter and MCP tool references
- [ ] `skills/find-pipelines-by-service/SKILL.md` exists with valid frontmatter
- [ ] `skills/adf-overview/SKILL.md` exists with valid frontmatter
- [ ] `uv run python -c "from agent_runtime.skills import SkillLoader; s = SkillLoader('.'); print(s.get_descriptions())"` shows 3 skills
- [ ] CLI: `uv run agent-cli -w .` startup banner shows MCP tool count (if ADF env vars set)
- [ ] CLI: `/skills` command lists 3 skills
- [ ] CLI: `/tools` command shows mcp_adf_* tools grouped under MCP section (if ADF connected)
- [ ] Web: `uv run agent-web -w .` and `/api/skills` returns 3 skills
- [ ] Web: `/api/tools` includes mcp_adf_* tools (if ADF connected)
- [ ] Web: tool calls with mcp_adf_ prefix render with blue Azure accent
- [ ] `uv run pytest tests/test_integration.py -v` — skill loading tests pass
- [ ] Both `uv run agent` (original) and `uv run agent-cli` and `uv run agent-web` still work

## What NOT to do
- Do NOT modify adf_mcp_server.py
- Do NOT modify agent_runtime/ (loop.py callback was added in Prompt 1, nothing more needed)
- Do NOT hardcode Azure credentials — env vars only
- Do NOT create duplicate ADF tool implementations — all calls go through MCP
- Do NOT add new Python dependencies

After all verification steps pass, output: <promise>COMPLETE</promise>
