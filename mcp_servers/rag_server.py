"""
rag_server.py

MCP server exposing RAG document management (list, add_directory, remove_directory).
"""

import os
import sys
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

mcp = FastMCP("rag")

_rag_manager = None
_personal_docs_manager = None
_initialized = False


def _ensure_init():
    """Lazy-init RAG managers on first use."""
    global _rag_manager, _personal_docs_manager, _initialized
    if _initialized:
        return
    _initialized = True

    try:
        from src.rag_singleton import get_rag_manager
        _rag_manager = get_rag_manager()
    except Exception:
        pass

    try:
        from src.constants import PERSONAL_DIR
        from src.personal_docs import PersonalDocsManager
        _personal_docs_manager = PersonalDocsManager(PERSONAL_DIR, _rag_manager)
    except Exception:
        pass


def _coerce_directory(value) -> str:
    """Coerce a tool argument to a stripped string directory path.

    The MCP spec doesn't pin argument types until the JSON Schema check, so a
    non-string `directory` (e.g. a number from a confused model) would crash
    on `.strip()`. Coercing to "" lets the existing "needs a directory path"
    error path fire uniformly. Annotated as `str | None` so FastMCP's client-
    side validation accepts nulls; we coerce in the helper, not at parse time.
    """
    return value.strip() if isinstance(value, str) else ""


@mcp.tool
def manage_rag(
    action: Literal["list", "add_directory", "remove_directory"],
    directory: str | None = None,
) -> str:
    """Manage RAG indexed documents. List indexed files, add directories, or remove directories."""
    _ensure_init()

    if action == "list":
        if not _personal_docs_manager:
            return "Personal docs manager not available. RAG may not be configured."
        try:
            files = getattr(_personal_docs_manager, "index", None) or []
            dirs = []
            if hasattr(_personal_docs_manager, "get_indexed_directories"):
                dirs = _personal_docs_manager.get_indexed_directories()

            lines = []
            if dirs:
                lines.append(f"**Indexed directories ({len(dirs)}):**")
                for d in dirs:
                    lines.append(f"  - `{d}`")
            if files:
                lines.append(f"\n**Indexed files ({len(files)}):**")
                for f in files[:50]:
                    fname = f.get("name", str(f)) if isinstance(f, dict) else str(f)
                    lines.append(f"  - {fname}")
                if len(files) > 50:
                    lines.append(f"  ... and {len(files) - 50} more")
            if not lines:
                return "No files or directories indexed in RAG."
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    if action == "add_directory":
        directory = _coerce_directory(directory)
        if not directory:
            return "Error: add_directory needs a directory path"
        # Store an absolute path so indexed `source` metadata is absolute and
        # remove_directory (which abspath-normalizes) can match it later (#1660).
        directory = os.path.abspath(os.path.expanduser(directory))
        if not os.path.isdir(directory):
            return f"Error: Directory not found: {directory}"
        if not _rag_manager:
            return "Error: RAG manager not available"
        try:
            result = _rag_manager.index_personal_documents(directory)
            indexed = result.get("indexed_count", 0) if isinstance(result, dict) else 0
            # Record the directory so `list` and `remove_directory` can see it.
            # Indexing was just done above, so pass index=False to avoid a second
            # (ownerless) pass. Without this the directory was indexed but never
            # tracked in indexed_directories, so it was invisible/unremovable.
            if _personal_docs_manager and hasattr(_personal_docs_manager, "add_directory"):
                try:
                    _personal_docs_manager.add_directory(directory, index=False)
                except Exception:
                    pass
            return f"Directory '{directory}' added to RAG index ({indexed} chunks indexed)"
        except Exception as e:
            return f"Error: Failed to index directory: {e}"

    if action == "remove_directory":
        directory = _coerce_directory(directory)
        if not directory:
            return "Error: remove_directory needs a directory path"
        # Expand ~ to match add_directory, which indexes the expanded path.
        # Without this, removing "~/docs" never matches the stored absolute path.
        directory = os.path.expanduser(directory)
        if not _personal_docs_manager:
            return "Error: Personal docs manager not available"
        try:
            if hasattr(_personal_docs_manager, "remove_directory"):
                _personal_docs_manager.remove_directory(directory)
            if _rag_manager and hasattr(_rag_manager, "remove_directory"):
                _rag_manager.remove_directory(directory)
            return f"Directory '{directory}' removed from RAG index"
        except Exception as e:
            return f"Error: Failed to remove directory: {e}"

    return f"Error: Unknown action '{action}'. Use: list, add_directory, remove_directory"


if __name__ == "__main__":
    mcp.run(transport="stdio")
