"""MCP client — connects to MCP servers, exposes their tools to the agent.

MCP is async; the agent loop is sync. This module bridges the gap with a
background event loop thread. All public methods are sync.

Config format (mcp.json):
{
  "servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {"Authorization": "Bearer ghp_xxx"}
    }
  }
}
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client
    from contextlib import AsyncExitStack
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


class MCPManager:
    """Manages connections to multiple MCP servers."""

    def __init__(self):
        self._servers: dict[str, ClientSession] = {}   # name → session
        self._tools: dict[str, str] = {}                # tool_name → server_name
        self._tool_schemas: list[dict] = []             # Anthropic-format schemas
        self._loop: asyncio.AbstractEventLoop = None
        self._thread: threading.Thread = None
        self._exit_stack: AsyncExitStack = None

    @property
    def available(self) -> bool:
        return MCP_AVAILABLE

    @property
    def tool_schemas(self) -> list[dict]:
        return list(self._tool_schemas)

    @property
    def tool_names(self) -> set[str]:
        return set(self._tools.keys())

    def load_config(self, config_path: Path) -> dict:
        """Load MCP server config from JSON file."""
        if not config_path.exists():
            return {}
        return json.loads(config_path.read_text())

    def start(self, mcp_config: dict):
        """Start background event loop and connect to all configured servers."""
        if not MCP_AVAILABLE:
            print("\033[33m  [warn] `mcp` package not installed. Run: uv add mcp\033[0m")
            return
        servers = mcp_config.get("servers", {})
        if not servers:
            return

        # Start background event loop
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        # Connect to all servers
        future = asyncio.run_coroutine_threadsafe(
            self._connect_all(servers), self._loop
        )
        # Timeout is generous to allow OAuth browser flows
        future.result(timeout=180)

    async def _connect_all(self, servers: dict):
        """Connect to all MCP servers and collect their tools."""
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()

        for name, server_cfg in servers.items():
            try:
                await self._connect_one(name, server_cfg)
            except BaseException as e:
                print(f"  \033[33m[warn] MCP server '{name}' failed: {e}\033[0m")

    async def _connect_one(self, name: str, server_cfg: dict):
        """Connect to a single MCP server via stdio or HTTP (with optional OAuth)."""
        server_type = server_cfg.get("type", "stdio")

        if server_type == "http":
            url = server_cfg["url"]
            headers = server_cfg.get("headers", {})
            has_auth = any(k.lower() == "authorization" for k in headers)

            if not has_auth:
                raise ValueError(
                    f"HTTP MCP server '{name}' requires an Authorization header. "
                    f"OAuth is not supported — add a token to mcp.json:\n"
                    f'  "headers": {{"Authorization": "Bearer <your-token>"}}'
                )

            transport = await self._exit_stack.enter_async_context(
                streamablehttp_client(url, headers=headers)
            )
            read_stream, write_stream, _ = transport
        else:
            params = StdioServerParameters(
                command=server_cfg["command"],
                args=server_cfg.get("args", []),
                env=server_cfg.get("env"),
            )
            transport = await self._exit_stack.enter_async_context(
                stdio_client(params)
            )
            read_stream, write_stream = transport

        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._servers[name] = session

        # List tools and convert to Anthropic schema
        response = await session.list_tools()
        for tool in response.tools:
            # Prefix tool name with server name to avoid collisions
            qualified_name = f"mcp_{name}_{tool.name}"
            self._tools[qualified_name] = name
            self._tool_schemas.append({
                "name": qualified_name,
                "description": f"[MCP:{name}] {tool.description or tool.name}",
                "input_schema": tool.inputSchema if hasattr(tool, 'inputSchema') else
                               getattr(tool, 'input_schema', {"type": "object", "properties": {}}),
            })
        print(f"  MCP '{name}': {len(response.tools)} tools")

    def call_tool(self, qualified_name: str, args: dict) -> str:
        """Call an MCP tool (sync wrapper). Returns result as string."""
        server_name = self._tools.get(qualified_name)
        if not server_name:
            return f"Error: Unknown MCP tool '{qualified_name}'"
        session = self._servers.get(server_name)
        if not session:
            return f"Error: MCP server '{server_name}' not connected"

        # Strip the mcp_{server}_ prefix to get original tool name
        prefix = f"mcp_{server_name}_"
        original_name = qualified_name[len(prefix):]

        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(session, original_name, args),
            self._loop
        )
        try:
            return future.result(timeout=120)
        except TimeoutError:
            return "Error: MCP tool call timeout (120s)"
        except Exception as e:
            return f"Error: MCP tool call failed: {e}"

    async def _call_tool_async(self, session: ClientSession, name: str, args: dict) -> str:
        """Async tool call."""
        result = await session.call_tool(name, args)
        # MCP result.content is a list of content blocks
        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            else:
                parts.append(str(block))
        return "\n".join(parts) if parts else "(no output)"

    def shutdown(self):
        """Clean up all connections."""
        if self._loop and self._exit_stack:
            future = asyncio.run_coroutine_threadsafe(
                self._exit_stack.aclose(), self._loop
            )
            try:
                future.result(timeout=5)
            except Exception:
                pass
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3)
