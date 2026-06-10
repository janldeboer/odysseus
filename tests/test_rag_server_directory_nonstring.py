"""Regression: rag_server add/remove_directory must not crash on a non-string path.

`directory = arguments.get("directory", "").strip()` ran before the surrounding
try in the old `@server.call_tool` dispatcher, so a non-string `directory` in
the tool args (e.g. a number) raised AttributeError out of call_tool. The new
@fastmcp-tool-based version routes the arg through `_coerce_directory` first,
which silently turns non-strings into "" so the existing "needs a directory
path" error fires uniformly.
"""
import pytest

pytest.importorskip("mcp")

import mcp_servers.rag_server as rs


def _manage_rag(action, directory):
    """Invoke the @mcp.tool function the way fastmcp would dispatch it."""
    return rs.manage_rag(action=action, directory=directory)


def test_add_directory_non_string_does_not_crash():
    out = _manage_rag(action="add_directory", directory=123)
    assert "needs a directory path" in out


def test_remove_directory_non_string_does_not_crash():
    out = _manage_rag(action="remove_directory", directory=["x"])
    assert "needs a directory path" in out
