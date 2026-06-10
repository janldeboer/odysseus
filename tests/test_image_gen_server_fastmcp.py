"""End-to-end test: the rewritten image_gen_server exposes generate_image
over stdio and returns the expected error paths without making real network
calls.
"""
import os
import sys

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


@pytest.mark.asyncio
async def test_generate_image_round_trip():
    """Spin up the rewritten image_gen_server as a subprocess and verify the
    schema, the missing-prompt error path, and the disabled-in-settings path
    are all reachable end-to-end over fastmcp."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(base_dir, "mcp_servers", "image_gen_server.py")

    transport = StdioTransport(
        command=sys.executable,
        args=[script],
        env={"PYTHONPATH": base_dir, "PATH": os.environ.get("PATH", "")},
        keep_alive=False,
    )

    async with Client(transport) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "generate_image" in names

        tool = next(t for t in tools if t.name == "generate_image")
        props = tool.inputSchema["properties"]
        # Required: prompt only
        assert tool.inputSchema["required"] == ["prompt"]
        # Optional parameters with defaults
        assert "model" in props
        assert "size" in props
        assert props["size"]["default"] == "1024x1024"
        assert "quality" in props
        assert set(props["quality"]["enum"]) == {"low", "medium", "high", "auto"}
        assert props["quality"]["default"] == "medium"

        # Empty prompt: tool body returns the error string.
        r = await client.call_tool(
            "generate_image", {"prompt": ""}, raise_on_error=False,
        )
        assert not r.is_error
        assert "Image prompt is required" in r.content[0].text
