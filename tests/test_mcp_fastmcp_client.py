"""Round-trip tests for the fastmcp.Client-based McpManager.

These exercise the new connection path end-to-end against:
  1. a real low-level mcp.server.Server (matching the built-in servers' style)
  2. an in-memory fastmcp.FastMCP server (the FastMCP-native happy path)
  3. a bad-command failure to verify the 'error' status is published

Each test asserts the public-surface contract that the agent loop,
tool_execution, and admin routes depend on (status shape, tools list,
call_tool result dict, generation bump).
"""
import os
import sys
import tempfile

import pytest

pytest.importorskip("fastmcp")

from src.mcp_manager import McpManager  # noqa: E402


LOW_LEVEL_SCRIPT = '''
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("lowlevel")

@server.list_tools()
async def list_tools():
    return [Tool(
        name="echo",
        description="Echo the input back",
        inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )]

@server.call_tool()
async def call_tool(name, arguments):
    if name == "echo":
        return [TextContent(type="text", text=f"echo: {arguments.get('text', '')}")]
    return [TextContent(type="text", text=f"unknown: {name}")]

async def run():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())

asyncio.run(run())
'''


@pytest.fixture
def low_level_server_path():
    fd, path = tempfile.mkstemp(suffix=".py")
    os.write(fd, LOW_LEVEL_SCRIPT.encode())
    os.close(fd)
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_connect_and_call_lowlevel_stdio(low_level_server_path):
    """Connect to a real low-level stdio MCP server via fastmcp.Client and
    exercise the full public surface: status, tools, call_tool, disconnect."""
    mgr = McpManager()
    initial_gen = mgr._generation
    ok = await mgr.connect_server(
        server_id="t1", name="Test", transport="stdio",
        command=sys.executable, args=[low_level_server_path], env={},
    )
    assert ok is True
    status = mgr.get_server_status("t1")
    assert status["status"] == "connected"
    assert status["name"] == "Test"
    assert status["transport"] == "stdio"
    assert status["tool_count"] == 1

    tools = mgr.get_all_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "echo"
    assert tools[0]["qualified_name"] == "mcp__t1__echo"
    assert tools[0]["server_name"] == "Test"

    result = await mgr.call_tool("mcp__t1__echo", {"text": "hi"})
    assert result == {"stdout": "echo: hi", "stderr": "", "exit_code": 0}

    await mgr.disconnect_server("t1")
    assert mgr.get_server_status("t1")["status"] == "disconnected"
    assert mgr._generation > initial_gen


@pytest.mark.asyncio
async def test_call_tool_unknown_server_returns_error():
    """Calling a non-connected server returns a clean error dict, not a crash."""
    mgr = McpManager()
    result = await mgr.call_tool("mcp__nope__missing", {"x": 1})
    assert result["exit_code"] == 1
    assert "not connected" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_invalid_qualified_name():
    mgr = McpManager()
    result = await mgr.call_tool("not_mcp_qualified", {})
    assert result["exit_code"] == 1
    assert "Invalid MCP tool name" in result["error"]


@pytest.mark.asyncio
async def test_connect_failure_publishes_error_status():
    """A non-existent command publishes status='error' (not a crash) and bumps
    the generation so the prompt cache invalidates."""
    mgr = McpManager()
    initial_gen = mgr._generation
    ok = await mgr.connect_server(
        server_id="bad", name="Bad", transport="stdio",
        command="/nonexistent/binary", args=[], env={},
    )
    assert ok is False
    status = mgr.get_server_status("bad")
    assert status["status"] == "error"
    assert "error" in status
    assert status["name"] == "Bad"
    assert mgr._generation > initial_gen


@pytest.mark.asyncio
async def test_reconnect_replaces_client(low_level_server_path):
    """Disconnect + reconnect yields a fresh client (not a stale one)."""
    mgr = McpManager()
    await mgr.connect_server(
        server_id="t2", name="Test2", transport="stdio",
        command=sys.executable, args=[low_level_server_path], env={},
    )
    first = mgr._clients["t2"]
    await mgr.disconnect_server("t2")
    await mgr.connect_server(
        server_id="t2", name="Test2", transport="stdio",
        command=sys.executable, args=[low_level_server_path], env={},
    )
    second = mgr._clients["t2"]
    assert first is not second
    # And a call still works on the new client.
    result = await mgr.call_tool("mcp__t2__echo", {"text": "again"})
    assert result["stdout"] == "echo: again"
    await mgr.disconnect_server("t2")


@pytest.mark.asyncio
async def test_get_all_openai_schemas_namespaced(low_level_server_path):
    mgr = McpManager()
    await mgr.connect_server(
        server_id="t3", name="Test3", transport="stdio",
        command=sys.executable, args=[low_level_server_path], env={},
    )
    schemas = mgr.get_all_openai_schemas()
    assert len(schemas) == 1
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] == "mcp__t3__echo"
    assert "[MCP:Test3]" in schemas[0]["function"]["description"]
    await mgr.disconnect_server("t3")


@pytest.mark.asyncio
async def test_plan_mode_blocks_writes_when_no_readonly_hint(low_level_server_path):
    """Low-level servers with no readOnlyHint annotation: plan mode falls back
    to the name heuristic. 'echo' starts with no known read-verb, so it should
    be blocked."""
    mgr = McpManager()
    await mgr.connect_server(
        server_id="t4", name="Test4", transport="stdio",
        command=sys.executable, args=[low_level_server_path], env={},
    )
    disabled_map, qualified = mgr.plan_mode_blocked_mcp()
    assert "mcp__t4__echo" in qualified
    assert "echo" in disabled_map.get("t4", set())
    await mgr.disconnect_server("t4")


@pytest.mark.asyncio
async def test_identity_hints_pulled_from_env(low_level_server_path):
    """Identity labels (e.g. email_address env var) end up in connection status
    so the admin UI can disambiguate multiple accounts of the same server."""
    mgr = McpManager()
    await mgr.connect_server(
        server_id="t5", name="Test5", transport="stdio",
        command=sys.executable, args=[low_level_server_path],
        env={"EMAIL_ADDRESS": "alice@example.com", "PATH": "/usr/bin"},
    )
    status = mgr.get_server_status("t5")
    assert status["identity"] == "alice@example.com"
    await mgr.disconnect_server("t5")


@pytest.mark.asyncio
async def test_image_content_returns_images_in_result():
    """Image content blocks (e.g. Playwright screenshots) flow through
    _result_to_dict with a [Screenshot captured (...)] line and a separate
    `images` list — exactly the contract the agent loop relies on."""
    from src.mcp_manager import _result_to_dict

    class Img:
        type = "image"
        mimeType = "image/png"
        data = b"\x89PNG_FAKE"

    class R:
        content = [Img()]
        is_error = False
        structured_content = None
        data = None

    result = _result_to_dict(R())
    assert result["exit_code"] == 0
    assert result["stdout"] == "[Screenshot captured (image/png)]"
    assert result["images"] == [{"data": b"\x89PNG_FAKE", "mimeType": "image/png"}]


@pytest.mark.asyncio
async def test_error_result_routes_to_stderr():
    """is_error=True puts the output in stderr and flips exit_code to 1."""
    from src.mcp_manager import _result_to_dict

    class T:
        type = "text"
        text = "boom"

    class R:
        content = [T()]
        is_error = True
        structured_content = None
        data = None

    result = _result_to_dict(R())
    assert result["stdout"] == ""
    assert result["stderr"] == "boom"
    assert result["exit_code"] == 1


@pytest.mark.asyncio
async def test_structured_content_fallback_to_data():
    """If a fastmcp tool returns only structured_content (no text blocks),
    _result_to_dict JSON-encodes it into stdout."""
    from src.mcp_manager import _result_to_dict

    class R:
        content = []
        is_error = False
        structured_content = {"result": 42}
        data = None

    result = _result_to_dict(R())
    assert result["exit_code"] == 0
    assert '"result": 42' in result["stdout"]


@pytest.mark.asyncio
async def test_inmemory_fastmcp_server_round_trip():
    """End-to-end with a fastmcp.FastMCP server in the same process — this
    exercises the in-memory transport, which uses a different code path than
    stdio but should produce the same public-surface results."""
    from fastmcp import FastMCP, Client
    from src.mcp_manager import _tool_to_spec

    mcp = FastMCP("InMem")

    @mcp.tool
    def add(a: int, b: int) -> int:
        """Add two numbers"""
        return a + b

    async with Client(mcp) as c:
        tools = await c.list_tools()
        specs = [_tool_to_spec(t) for t in tools]
        assert len(specs) == 1
        assert specs[0]["name"] == "add"
        assert specs[0]["description"] == "Add two numbers"
        assert "a" in specs[0]["input_schema"]["properties"]
        result = await c.call_tool("add", {"a": 2, "b": 3})
        # _result_to_dict on a real CallToolResult
        from src.mcp_manager import _result_to_dict
        d = _result_to_dict(result)
        assert d["stdout"] == "5"
        assert d["exit_code"] == 0
