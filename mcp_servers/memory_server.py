"""
memory_server.py

MCP server exposing memory management (list, add, edit, delete, search).
Imports MemoryManager and MemoryVectorStore from the Odysseus codebase.
"""

import sys
import time
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

mcp = FastMCP("memory")

# Late-initialized managers (set during first tool call)
_memory_manager = None
_memory_vector = None
_initialized = False


def _ensure_init():
    """Lazy-init memory managers on first use."""
    global _memory_manager, _memory_vector, _initialized
    if _initialized:
        return
    _initialized = True

    from src.constants import DATA_DIR
    from src.memory import MemoryManager
    _memory_manager = MemoryManager(DATA_DIR)

    try:
        from src.memory_vector import MemoryVectorStore
        _memory_vector = MemoryVectorStore(DATA_DIR)
        if not _memory_vector.healthy:
            _memory_vector = None
    except Exception:
        _memory_vector = None


@mcp.tool
def manage_memory(
    action: Literal["list", "add", "edit", "delete", "search"],
    text: str = "",
    memory_id: str = "",
    category: str | None = None,
) -> str:
    """Manage the user's memory system: list, add, edit, delete, or search memories.

    `category` is one of "fact", "event", "contact", "preference" for `add`
    (default "fact") and a filter for `list` (None = no filter, list
    everything — matches the pre-FastMCP default).
    """
    _ensure_init()
    if not _memory_manager:
        return "Error: Memory manager not available"

    if action == "list":
        memories = _memory_manager.load()
        if category:
            memories = [m for m in memories if m.get("category", "").lower() == category.lower()]
        if not memories:
            msg = "No memories found"
            if category:
                msg += f" in category '{category}'"
            return msg + "."
        lines = [f"Found {len(memories)} memory entries:\n"]
        for m in memories[:100]:
            cat = m.get("category", "fact")
            mid = m.get("id", "?")[:8]
            text0 = m.get("text", "")
            if len(text0) > 150:
                text0 = text0[:150] + "..."
            lines.append(f"- [{cat}] `{mid}` — {text0}")
        if len(memories) > 100:
            lines.append(f"... and {len(memories) - 100} more")
        return "\n".join(lines)

    if action == "add":
        if not text:
            return "Error: Memory text cannot be empty"
        # category defaults to "fact" for add; None means the caller didn't pick.
        add_category = category or "fact"
        entry = _memory_manager.add_entry(text, source="ai_agent", category=add_category)
        memories = _memory_manager.load_all()
        memories.append(entry)
        _memory_manager.save(memories)
        if _memory_vector and _memory_vector.healthy:
            try:
                _memory_vector.add(entry["id"], text)
            except Exception:
                pass
        return f"Memory added: [{add_category}] {text} (id: {entry['id'][:8]})"

    if action == "edit":
        if not memory_id or not text:
            return "Error: edit needs memory_id and text"
        memories = _memory_manager.load_all()
        found = False
        full_id = None
        for m in memories:
            if m.get("id", "").startswith(memory_id):
                m["text"] = text
                m["timestamp"] = int(time.time())
                found = True
                full_id = m["id"]
                break
        if not found:
            return f"Error: Memory '{memory_id}' not found"
        _memory_manager.save(memories)
        if _memory_vector and _memory_vector.healthy and full_id:
            try:
                _memory_vector.remove(full_id)
                _memory_vector.add(full_id, text)
            except Exception:
                pass
        return f"Memory updated: {text}"

    if action == "delete":
        if not memory_id:
            return "Error: delete needs memory_id"
        memories = _memory_manager.load_all()
        full_id = None
        deleted_text = ""
        deleted_category = ""
        for m in memories:
            if m.get("id", "").startswith(memory_id):
                full_id = m["id"]
                deleted_text = m.get("text", "")
                deleted_category = m.get("category", "")
                break
        if not full_id:
            return f"Error: Memory '{memory_id}' not found"
        memories = [m for m in memories if m.get("id") != full_id]
        _memory_manager.save(memories)
        if _memory_vector and _memory_vector.healthy and full_id:
            try:
                _memory_vector.remove(full_id)
            except Exception:
                pass
        cat = f"[{deleted_category}] " if deleted_category else ""
        snippet = deleted_text if len(deleted_text) <= 120 else deleted_text[:117] + "..."
        return f"Memory deleted: {cat}{snippet} (id: {memory_id})"

    if action == "search":
        if not text:
            return "Error: search needs text (query)"
        memories = _memory_manager.load()
        if hasattr(_memory_manager, "get_relevant_memories"):
            results = _memory_manager.get_relevant_memories(query=text, memories=memories, threshold=0.05, max_items=20)
        else:
            query_lower = text.lower()
            results = [m for m in memories if query_lower in m.get("text", "").lower()][:20]
        if not results:
            return f"No memories found matching '{text}'."
        lines = [f"Found {len(results)} matching memories:\n"]
        for m in results:
            cat = m.get("category", "fact")
            mid = m.get("id", "?")[:8]
            t = m.get("text", "")
            lines.append(f"- [{cat}] `{mid}` — {t}")
        return "\n".join(lines)

    return f"Error: Unknown action '{action}'. Use: list, add, edit, delete, search"


if __name__ == "__main__":
    mcp.run(transport="stdio")
