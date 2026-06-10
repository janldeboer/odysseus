"""End-to-end test: the rewritten memory_server exposes manage_memory over
stdio and returns the expected error paths without needing a real
MemoryManager backend (init failure path) and the schema with the
correct enums.
"""
import os
import sys

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


@pytest.mark.asyncio
async def test_manage_memory_round_trip():
    """Spin up the rewritten memory_server as a subprocess and verify the
    schema, the empty-text-on-add error, and the search-needs-text error."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(base_dir, "mcp_servers", "memory_server.py")

    transport = StdioTransport(
        command=sys.executable,
        args=[script],
        env={"PYTHONPATH": base_dir, "PATH": os.environ.get("PATH", "")},
        keep_alive=False,
    )

    async with Client(transport) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "manage_memory" in names

        tool = next(t for t in tools if t.name == "manage_memory")
        props = tool.inputSchema["properties"]

        # action is a required string enum.
        assert tool.inputSchema["required"] == ["action"]
        assert set(props["action"]["enum"]) == {"list", "add", "edit", "delete", "search"}

        # Optional parameters: text, memory_id, category (no required, no
        # default filtering by category for `list`).
        assert "text" in props
        assert "memory_id" in props
        assert "category" in props

        # add with empty text returns the body error, not a 5xx.
        r = await client.call_tool(
            "manage_memory", {"action": "add", "text": ""}, raise_on_error=False,
        )
        assert not r.is_error
        assert "Memory text cannot be empty" in r.content[0].text

        # edit without text/memory_id also error path.
        r = await client.call_tool(
            "manage_memory", {"action": "edit"}, raise_on_error=False,
        )
        assert not r.is_error
        assert "edit needs memory_id and text" in r.content[0].text

        # delete without memory_id error path.
        r = await client.call_tool(
            "manage_memory", {"action": "delete"}, raise_on_error=False,
        )
        assert not r.is_error
        assert "delete needs memory_id" in r.content[0].text

        # search without text error path.
        r = await client.call_tool(
            "manage_memory", {"action": "search"}, raise_on_error=False,
        )
        assert not r.is_error
        assert "search needs text" in r.content[0].text
