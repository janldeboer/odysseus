"""
mcp_manager.py

Manages connections to MCP (Model Context Protocol) tool servers.
Each server exposes tools that are made available to the agent loop.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

def _format_mcp_connection_error(name: str, command: str = "", args: Optional[List[str]] = None, error: Exception = None) -> str:
    """Return a user-actionable MCP connection error message."""
    args = args or []
    raw_error = str(error) if error else "Unknown error"
    command_line = " ".join([command or "", *args]).strip()
    lower_command = command_line.lower()

    if "@playwright/mcp" in lower_command:
        return (
            f"{raw_error}\n\n"
            "Browser MCP could not start. On fresh installs, cache the Playwright MCP package once before connecting:\n\n"
            "npx -y @playwright/mcp@latest --version\n\n"
            "Then restart Odysseus and reconnect the Browser MCP server."
        )

    return raw_error


# Caps for rendering untrusted MCP tool schemas into the agent prompt (issue #2660).
# MCP servers are third-party/user-added, so field names and parameter counts are
# untrusted input — bound them so an odd or hostile schema cannot distort the prompt.
_MCP_PARAM_MAX = 12   # max params rendered per tool
_MCP_TOKEN_MAX = 40   # max chars per rendered name / type token
_MCP_HINT_MAX = 300   # total-length backstop for the whole hint


def _sanitize_schema_token(value: Any, limit: int = _MCP_TOKEN_MAX) -> str:
    """Make an untrusted JSON-Schema token safe to splice into the prompt.

    Replaces control chars / newlines with a space, collapses whitespace, and
    length-caps the result, so a weird field name or type cannot inject newlines
    or run on. Normal short identifiers pass through unchanged.
    """
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _format_mcp_params(input_schema: Any) -> str:
    """Render an MCP tool's JSON-Schema inputs as a compact prompt hint.

    Without this the agent only sees a tool's name + description and has to
    guess its arguments (issue #2509). Produces e.g.
    ` Args (JSON): {"path": string (required), "limit": integer}` — names,
    coarse types, and required-ness, kept short so it stays prompt-friendly.
    Returns "" when there are no parameters.

    MCP servers are third-party, so names/types are sanitized and the parameter
    count + total length are capped (issue #2660); normal schemas are unaffected.
    """
    if not isinstance(input_schema, dict):
        return ""
    props = input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(input_schema.get("required") or [])
    parts = []
    for pname, pinfo in list(props.items())[:_MCP_PARAM_MAX]:
        pinfo = pinfo if isinstance(pinfo, dict) else {}
        ptype = pinfo.get("type") or "any"
        if isinstance(ptype, list):
            ptype = "|".join(str(x) for x in ptype)
        tag = f'"{_sanitize_schema_token(pname)}": {_sanitize_schema_token(ptype)}'
        if pname in required:
            tag += " (required)"
        parts.append(tag)
    extra = len(props) - len(parts)
    if extra > 0:
        parts.append(f"…+{extra} more")
    hint = " Args (JSON): {" + ", ".join(parts) + "}"
    if len(hint) > _MCP_HINT_MAX:
        hint = hint[:_MCP_HINT_MAX - 1].rstrip() + "…"
    return hint


# Tool-name prefixes that denote a read-only/inspection operation. Used to
# classify MCP tools for plan mode when the server provides no readOnlyHint.
# These are PREFIXES, not whole words (matched via str.startswith below), so a
# stem like "summar" intentionally covers "summarise"/"summarize"/"summary".
_MCP_READONLY_VERBS = (
    "list", "get", "read", "search", "fetch", "query", "find", "describe",
    "show", "view", "lookup", "count", "status", "info", "inspect", "summar",
)


def mcp_tool_is_readonly(tool: Dict) -> bool:
    """Classify an MCP tool as safe (non-mutating) for plan mode.

    Prefer the server's own annotations (readOnlyHint / destructiveHint). When
    absent, fall back to a tool-name verb heuristic, and FAIL CLOSED (treat as
    write) for anything that doesn't clearly read — plan mode must not run a
    write tool just because its intent is ambiguous.
    """
    ann = tool.get("annotations")
    # annotations may be a dict or a pydantic model
    read_hint = None
    destructive = None
    if ann is not None:
        if isinstance(ann, dict):
            read_hint = ann.get("readOnlyHint")
            destructive = ann.get("destructiveHint")
        else:
            read_hint = getattr(ann, "readOnlyHint", None)
            destructive = getattr(ann, "destructiveHint", None)
    if read_hint is True:
        return True
    if read_hint is False or destructive is True:
        return False
    # No usable hint — heuristic on the tool name's leading verb.
    name = (tool.get("name") or "").lower()
    return name.startswith(_MCP_READONLY_VERBS)


def _identity_hints_from_env(env: Optional[Dict[str, str]]) -> str:
    """Pull identity labels (email, username, account) out of stdio env vars.

    Used to disambiguate multiple instances of the same MCP server (e.g. two
    email accounts) in tool descriptions and status UI.
    """
    if not env:
        return ""
    hints = []
    for k, v in env.items():
        k_lower = k.lower()
        if any(x in k_lower for x in ('email_address', 'account', 'user', 'username')):
            hints.append(v)
    return ", ".join(h for h in hints if h)


def _tool_to_spec(tool: Any) -> Dict[str, Any]:
    """Convert a fastmcp/MCP Tool object into our internal tool-schema dict.

    The shape is what the agent loop, admin routes, and prompt-cache code
    already consume: {name, description, input_schema, annotations}.
    """
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {},
        "annotations": getattr(tool, "annotations", None),
    }


def _transport_for(transport: str, *, command: Optional[str], args: Optional[List[str]],
                   env: Optional[Dict[str, str]], url: Optional[str],
                   auth: Optional[Any] = None) -> Any:
    """Build the right fastmcp transport for the given MCP server config.

    All stdio servers get a fresh subprocess per connect (keep_alive=False) so
    disconnect/reconnect semantics match the previous AsyncExitStack lifecycle
    exactly — a closed client is a dead subprocess, no shared state.
    """
    if transport == "stdio":
        from fastmcp.client.transports import StdioTransport
        return StdioTransport(
            command=command or "",
            args=list(args or []),
            env={**os.environ, **(env or {})} if env else None,
            keep_alive=False,
        )
    if transport == "sse":
        from fastmcp.client.transports import SSETransport
        return SSETransport(url=url or "", auth=auth)
    if transport == "http":
        from fastmcp.client.transports import StreamableHttpTransport
        return StreamableHttpTransport(url=url or "", auth=auth)
    raise ValueError(f"Unknown MCP transport: {transport}")


def _result_to_dict(result: Any) -> Dict[str, Any]:
    """Convert a fastmcp CallToolResult into the {stdout, stderr, exit_code, images}
    dict every call site (agent_loop, tool_execution, task_scheduler) expects.

    Mirrors the old _do_call behavior, with one addition: structured_content is
    JSON-encoded into stdout when no text content blocks are present, so
    FastMCP-style structured outputs reach the agent unchanged.
    """
    output_parts: List[str] = []
    images: List[Dict[str, Any]] = []
    for content in (result.content or []):
        ctype = getattr(content, "type", "")
        if ctype == "text" and hasattr(content, "text"):
            output_parts.append(content.text)
        elif ctype == "image" and hasattr(content, "data"):
            mime = getattr(content, "mimeType", None) or getattr(content, "mime_type", None) or "image/png"
            images.append({"data": content.data, "mimeType": mime})
            output_parts.append(f"[Screenshot captured ({mime})]")
        elif hasattr(content, "data"):
            output_parts.append(str(content.data))

    is_error = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    if not output_parts and not is_error:
        structured = getattr(result, "structured_content", None)
        if structured:
            output_parts.append(json.dumps(structured))
        elif getattr(result, "data", None) is not None:
            output_parts.append(str(result.data))

    output = "\n".join(output_parts)
    result_dict: Dict[str, Any] = {
        "stdout": "" if is_error else output,
        "stderr": output if is_error else "",
        "exit_code": 1 if is_error else 0,
    }
    if images:
        result_dict["images"] = images
    return result_dict


class McpManager:
    """Manages MCP server connections and tool routing.

    Connection lifecycle is delegated to fastmcp.Client; we keep the same
    public surface (connect_server / call_tool / get_all_tools / etc.) so
    callers across agent_loop, tool_execution, task_scheduler, routes, and
    builtin_mcp don't have to change.
    """

    def __init__(self):
        # server_id -> connection state dict (status, name, transport, tool_count, identity, auth_url, error)
        self._connections: Dict[str, Dict[str, Any]] = {}
        # server_id -> list of tool schemas (same shape as before: {name, description, input_schema, annotations})
        self._tools: Dict[str, List[Dict[str, Any]]] = {}
        # server_id -> fastmcp.Client (entered/active) or None if disconnected
        self._clients: Dict[str, Any] = {}
        # server_id -> background connect task (HTTP transport / OAuth)
        self._connect_tasks: Dict[str, Any] = {}
        # Tracking updates to tools/connections for RAG indexing / prompt cache
        self._generation = 0

    async def connect_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
    ) -> bool:
        """Connect to an MCP server via stdio, SSE, or Streamable HTTP transport."""
        try:
            if transport == "stdio":
                res = await self._connect_stdio(server_id, name, command, args or [], env or {})
            elif transport == "sse":
                res = await self._connect_sse(server_id, name, url)
            elif transport == "http":
                res = await self._start_http_connect(server_id, name, url)
            else:
                logger.error(f"Unknown MCP transport: {transport}")
                res = False
            if res:
                self._generation += 1
            return res
        except Exception as e:
            logger.error(f"Failed to connect MCP server {name} ({server_id}): {e}")
            error_message = _format_mcp_connection_error(name, command or "", args or [], e)
            self._connections[server_id] = {"status": "error", "error": error_message, "name": name}
            self._generation += 1
            return False

    async def _open_client(self, transport: str, *, server_id: str, name: str,
                           command: Optional[str] = None, args: Optional[List[str]] = None,
                           env: Optional[Dict[str, str]] = None, url: Optional[str] = None,
                           auth: Optional[Any] = None) -> Optional[Tuple[Any, List[Dict[str, Any]]]]:
        """Open a fastmcp.Client, run initialize + list_tools, return (client, tools).

        Returns None and publishes an 'error' status if anything goes wrong
        (caller checks _connections[server_id] to surface the message). On
        failure we also bump _generation so any cached tool prompt is
        invalidated — a failed connect is a structural state change.
        """
        try:
            from fastmcp import Client
        except ImportError:
            self._connections[server_id] = {"status": "error", "error": "fastmcp not installed", "name": name}
            self._generation += 1
            return None

        client = Client(
            _transport_for(transport, command=command, args=args, env=env, url=url, auth=auth),
            auto_initialize=True,
        )
        try:
            await client.__aenter__()
        except Exception as e:
            self._connections[server_id] = {"status": "error", "error": str(e), "name": name}
            self._generation += 1
            return None
        try:
            tools_raw = await client.list_tools()
        except Exception as e:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass
            self._connections[server_id] = {"status": "error", "error": str(e), "name": name}
            self._generation += 1
            return None
        return client, [_tool_to_spec(t) for t in tools_raw]

    async def _connect_stdio(self, server_id: str, name: str, command: str, args: List[str], env: Dict[str, str]) -> bool:
        """Connect to an MCP server via stdio transport."""
        result = await self._open_client(
            "stdio", server_id=server_id, name=name, command=command, args=args, env=env,
        )
        if result is None:
            return False
        client, tools = result
        self._clients[server_id] = client
        self._tools[server_id] = tools
        self._connections[server_id] = {
            "status": "connected",
            "name": name,
            "transport": "stdio",
            "tool_count": len(tools),
            "identity": _identity_hints_from_env(env),
        }
        logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via stdio")
        return True

    async def _connect_sse(self, server_id: str, name: str, url: str) -> bool:
        """Connect to an MCP server via SSE transport."""
        result = await self._open_client("sse", server_id=server_id, name=name, url=url)
        if result is None:
            return False
        client, tools = result
        self._clients[server_id] = client
        self._tools[server_id] = tools
        self._connections[server_id] = {
            "status": "connected",
            "name": name,
            "transport": "sse",
            "tool_count": len(tools),
        }
        logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via SSE")
        return True

    async def _start_http_connect(self, server_id: str, name: str, url: str, wait: float = 8.0) -> bool:
        """Begin a Streamable HTTP connect in the background. Returns within
        `wait` seconds: True if it connected (cached-token path), otherwise the
        flow is awaiting browser authorization and status becomes 'needs_auth'."""
        import asyncio
        self._connections[server_id] = {"status": "connecting", "name": name, "transport": "http"}
        task = asyncio.create_task(self._connect_http(server_id, name, url))
        self._connect_tasks[server_id] = task
        done, _ = await asyncio.wait({task}, timeout=wait)
        if task in done:
            try:
                return task.result()
            except Exception as e:
                self._connections[server_id] = {"status": "error", "error": str(e), "name": name}
                return False
        # Still running → either awaiting authorization, or discovery/DCR is
        # still in flight. If _on_redirect already published needs_auth+auth_url,
        # leave it; otherwise mark needs_auth (auth_url filled in once it fires).
        from src.mcp_oauth import pop_auth_url
        cur = self._connections.get(server_id, {})
        if cur.get("status") != "needs_auth":
            self._connections[server_id] = {
                "status": "needs_auth", "name": name, "transport": "http",
                "auth_url": pop_auth_url(server_id),
            }
        return False

    async def _connect_http(self, server_id: str, name: str, url: str) -> bool:
        """Connect to a Streamable HTTP MCP server (with automatic OAuth)."""
        from src.mcp_oauth import build_provider, clear_auth_url

        def _on_redirect(auth_url):
            # Publish needs_auth the moment the URL is known, independent of
            # how long discovery/DCR took (may exceed the bounded start wait).
            self._connections[server_id] = {
                "status": "needs_auth", "name": name, "transport": "http",
                "auth_url": auth_url,
            }

        try:
            provider = build_provider(server_id, url, on_redirect=_on_redirect)
        except ImportError:
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False

        result = await self._open_client("http", server_id=server_id, name=name, url=url, auth=provider)
        if result is None:
            return False
        client, tools = result
        self._clients[server_id] = client
        self._tools[server_id] = tools
        self._connections[server_id] = {
            "status": "connected", "name": name, "transport": "http",
            "tool_count": len(tools),
        }
        clear_auth_url(server_id)
        # Tools changed (this can complete after connect_server already
        # returned, via the background OAuth flow), so bump the generation
        # to invalidate the tool-prompt cache.
        self._generation += 1
        logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via http")
        return True

    async def disconnect_server(self, server_id: str):
        """Disconnect from an MCP server."""
        # Cancel any in-flight HTTP/OAuth background connect so it stops
        # publishing status for a server that may be getting deleted.
        task = self._connect_tasks.pop(server_id, None)
        if task is not None and not task.done():
            task.cancel()
        try:
            from src.mcp_oauth import clear_auth_url
            clear_auth_url(server_id)
        except Exception:
            pass

        client = self._clients.pop(server_id, None)
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing MCP server {server_id}: {e}")

        self._tools.pop(server_id, None)
        self._connections.pop(server_id, None)
        self._generation += 1
        logger.info(f"MCP server disconnected: {server_id}")

    async def disconnect_all(self):
        """Disconnect from all MCP servers."""
        ids = list(self._clients.keys())
        for sid in ids:
            await self.disconnect_server(sid)

    async def connect_all_enabled(self):
        """Connect to all enabled MCP servers from the database."""
        from src.database import McpServer, SessionLocal

        db = SessionLocal()
        try:
            servers = db.query(McpServer).filter(McpServer.is_enabled == True).all()
            for srv in servers:
                args = json.loads(srv.args) if srv.args else []
                env = json.loads(srv.env) if srv.env else {}
                await self.connect_server(
                    server_id=srv.id,
                    name=srv.name,
                    transport=srv.transport,
                    command=srv.command,
                    args=args,
                    env=env,
                    url=srv.url,
                )
        finally:
            db.close()

    async def call_tool(self, qualified_name: str, arguments: Dict) -> Dict:
        """Call an MCP tool by its qualified name (mcp__{server_id}__{tool_name}).

        Returns a result dict compatible with agent_tools format.
        """
        parts = qualified_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return {"error": f"Invalid MCP tool name: {qualified_name}", "exit_code": 1}

        server_id = parts[1]
        tool_name = parts[2]

        client = self._clients.get(server_id)
        if not client:
            return {"error": f"MCP server not connected: {server_id}", "exit_code": 1}

        try:
            result = await client.call_tool(tool_name, arguments or {})
        except Exception as e:
            # Auto-reconnect for builtin servers whose subprocess may have died
            if self.is_builtin(server_id):
                logger.warning(f"MCP call failed for {qualified_name}, attempting reconnect: {e}")
                reconnected = await self._reconnect_builtin(server_id)
                if reconnected:
                    client = self._clients.get(server_id)
                    if client:
                        try:
                            result = await client.call_tool(tool_name, arguments or {})
                        except Exception as e2:
                            logger.error(f"MCP tool call failed after reconnect: {qualified_name}: {e2}")
                            return {"error": str(e2), "exit_code": 1}
                    else:
                        return {"error": f"Reconnected but no client for {server_id}", "exit_code": 1}
                else:
                    logger.error(f"MCP reconnect failed for {server_id}")
                    return {"error": f"MCP server crashed and reconnect failed: {server_id}", "exit_code": 1}
            else:
                logger.error(f"MCP tool call failed: {qualified_name}: {e}")
                return {"error": str(e), "exit_code": 1}

        try:
            return _result_to_dict(result)
        except Exception as e:
            logger.error(f"MCP result conversion failed: {qualified_name}: {e}")
            return {"error": f"Bad MCP result: {e}", "exit_code": 1}

    async def _reconnect_builtin(self, server_id: str) -> bool:
        """Tear down and reconnect a crashed builtin MCP server."""
        import sys
        from src.builtin_mcp import _BUILTIN_SERVERS

        if server_id not in _BUILTIN_SERVERS:
            return False

        script_rel, name = _BUILTIN_SERVERS[server_id]
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script_path = os.path.join(base_dir, script_rel)

        # Clean up old connection
        await self.disconnect_server(server_id)

        try:
            ok = await self.connect_server(
                server_id=server_id,
                name=name,
                transport="stdio",
                command=sys.executable,
                args=[script_path],
                env={"PYTHONPATH": base_dir},
            )
            if ok:
                logger.info(f"Reconnected builtin MCP server: {name}")
            return ok
        except Exception as e:
            logger.error(f"Failed to reconnect builtin MCP server {name}: {e}")
            return False

    def get_all_openai_schemas(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return all MCP tools in OpenAI function-calling format.

        Tool names are namespaced as mcp__{server_id}__{tool_name}.
        disabled_map: optional {server_id: set_of_disabled_tool_names} to filter out.
        """
        schemas = []
        for server_id, tools in self._tools.items():
            # Skip builtin Python servers — they use the code-block tool format
            # But include NPX-based builtins (like browser) which need function calling
            if self.is_builtin(server_id) and server_id != "builtin_browser":
                continue
            conn = self._connections.get(server_id, {})
            server_name = conn.get("name", server_id)
            disabled = (disabled_map or {}).get(server_id, set())

            identity = conn.get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name

            for tool in tools:
                if tool["name"] in disabled:
                    continue
                qualified = f"mcp__{server_id}__{tool['name']}"
                schema = {
                    "type": "function",
                    "function": {
                        "name": qualified,
                        "description": f"[MCP:{label}] {tool['description']}",
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    },
                }
                schemas.append(schema)

        return schemas

    def get_all_tools(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return a flat list of all discovered tools with server info."""
        result = []
        for server_id, tools in self._tools.items():
            conn = self._connections.get(server_id, {})
            disabled = (disabled_map or {}).get(server_id, set())
            for tool in tools:
                result.append({
                    "server_id": server_id,
                    "server_name": conn.get("name", server_id),
                    "name": tool["name"],
                    "qualified_name": f"mcp__{server_id}__{tool['name']}",
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema") or {},
                    "is_disabled": tool["name"] in disabled,
                })
        return result

    def plan_mode_blocked_mcp(self) -> Tuple[Dict[str, Set[str]], Set[str]]:
        """Plan mode: block every MCP tool that isn't clearly read-only.

        Returns (disabled_map, qualified_names):
          - disabled_map: {server_id: {tool_name, ...}} to hide write tools from
            the prompt/schemas (merged into the existing mcp_disabled_map).
          - qualified_names: {"mcp__<server>__<tool>", ...} for runtime rejection
            in execute_tool_block (which matches the qualified name).
        """
        disabled_map: Dict[str, Set[str]] = {}
        qualified: Set[str] = set()
        for server_id, tools in self._tools.items():
            for tool in tools:
                if not mcp_tool_is_readonly(tool):
                    disabled_map.setdefault(server_id, set()).add(tool["name"])
                    qualified.add(f"mcp__{server_id}__{tool['name']}")
        return disabled_map, qualified

    def is_builtin(self, server_id: str) -> bool:
        """Check if a server is a built-in (auto-registered) server."""
        return server_id.startswith("builtin_") or server_id in {
            "image_gen",
            "memory",
            "rag",
            "email",
        }

    def get_server_status(self, server_id: str) -> Dict:
        """Get connection status for a server."""
        return self._connections.get(server_id, {"status": "disconnected"})

    def get_all_statuses(self) -> Dict[str, Dict]:
        """Get connection statuses for all servers."""
        return dict(self._connections)

    _cached_prompt_desc = None
    _cached_prompt_desc_key = None

    def get_tool_descriptions_for_prompt(self, disabled_map: Optional[Dict[str, set]] = None) -> str:
        """Generate text describing MCP tools for the agent system prompt. Cached."""
        cache_key = (
            frozenset((k, frozenset(v)) for k, v in (disabled_map or {}).items()),
            len(self._tools),
            self._generation,
        )
        if self._cached_prompt_desc is not None and self._cached_prompt_desc_key == cache_key:
            return self._cached_prompt_desc
        tools = self.get_all_tools(disabled_map)
        if not tools:
            return ""

        lines = ["\n\nYou also have access to external MCP tool servers. These tools are called via native function calling:"]
        by_server = {}
        for t in tools:
            # Skip builtin Python servers — they're already in the agent prompt
            # But include NPX-based builtins (like browser) which aren't hardcoded
            if self.is_builtin(t["server_id"]) and t["server_id"] != "builtin_browser":
                continue
            if t.get("is_disabled"):
                continue
            sn = t["server_name"]
            if sn not in by_server:
                by_server[sn] = []
            by_server[sn].append(t)

        if not by_server:
            return ""

        for server_name, server_tools in by_server.items():
            # Include identity (e.g. email address) if available
            sid = server_tools[0]["server_id"] if server_tools else ""
            identity = self._connections.get(sid, {}).get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name
            lines.append(f"\n**{label}:**")
            for t in server_tools:
                # Truncate long descriptions
                desc = t['description'][:120] + '...' if len(t['description']) > 120 else t['description']
                # Include the tool's declared inputs so the model calls it with
                # real argument names instead of guessing from the description
                # alone (issue #2509).
                args_hint = _format_mcp_params(t.get("input_schema"))
                lines.append(f"  - {t['qualified_name']}: {desc}{args_hint}")

        result = "\n".join(lines)
        self._cached_prompt_desc = result
        self._cached_prompt_desc_key = cache_key
        return result
