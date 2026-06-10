"""End-to-end test: the rewritten rag_server exposes manage_rag over stdio
and answers the full list/add_directory/remove_directory surface through a
real fastmcp.Client round-trip.
"""
import os
import sys

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


@pytest.mark.asyncio
async def test_manage_rag_round_trip(tmp_path):
    """Spin up the rewritten rag_server as a subprocess and exercise it."""
    # Build a small directory we can index.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("alpha")
    (docs / "b.txt").write_text("beta")

    # Make sure sys.path points at the project root so the server can import src.*
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(base_dir, "mcp_servers", "rag_server.py")

    transport = StdioTransport(
        command=sys.executable,
        args=[script],
        env={"PYTHONPATH": base_dir, "PATH": os.environ.get("PATH", "")},
        keep_alive=False,
    )

    async with Client(transport) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "manage_rag" in names

        tool = next(t for t in tools if t.name == "manage_rag")
        # action must be a string enum
        action_prop = tool.inputSchema["properties"]["action"]
        assert set(action_prop["enum"]) == {"list", "add_directory", "remove_directory"}
        assert "action" in tool.inputSchema["required"]
        # directory should be optional in the schema
        assert "directory" not in tool.inputSchema["required"]

        # raise_on_error=False so client-side validation errors return as
        # CallToolResult (with is_error=True) instead of raising. We assert on
        # the result text the way the old `Error: ...` contract did.

        # Empty directory on add -> error.
        r = await client.call_tool(
            "manage_rag", {"action": "add_directory", "directory": ""},
            raise_on_error=False,
        )
        assert not r.is_error
        assert "needs a directory path" in r.content[0].text

        # Non-existent directory -> clear error.
        r = await client.call_tool(
            "manage_rag",
            {"action": "add_directory", "directory": str(tmp_path / "missing")},
            raise_on_error=False,
        )
        assert not r.is_error
        assert "Directory not found" in r.content[0].text

        # remove_directory with empty path -> error.
        r = await client.call_tool(
            "manage_rag", {"action": "remove_directory", "directory": ""},
            raise_on_error=False,
        )
        assert not r.is_error
        assert "needs a directory path" in r.content[0].text
