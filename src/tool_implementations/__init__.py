"""tool_implementations package — split by domain for maintainability.

Each module contains related ``do_*`` tool implementation functions plus
their private helpers. Shared utilities (active-document tracking,
owner-scoped queries, MCP manager, internal loopback base, etc.) live
here so they can be imported across submodules without circular edges.

This package replaces the original 4476-line ``tool_implementations.py``
monolith. Every public name is re-exported below so existing imports
keep working unchanged, e.g.::

    from src.tool_implementations import do_manage_calendar
    from src.tool_implementations import set_active_document
"""
import logging

from src.constants import MAX_OUTPUT_CHARS
from core.constants import internal_api_base

logger = logging.getLogger(__name__)


def get_mcp_manager():
    from src import agent_tools
    return agent_tools.get_mcp_manager()


def _truncate(text, limit=MAX_OUTPUT_CHARS):
    if len(text) > limit:
        return text[:limit] + f"\n... (truncated, {len(text)} chars total)"
    return text


def _parse_tool_args(content):
    import json
    if isinstance(content, str):
        try:
            args = json.loads(content) if content.strip() else {}
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(str(e))
    elif isinstance(content, dict):
        args = content
    else:
        args = {}
    if (
        isinstance(args, dict)
        and len(args) == 1
        and "body" in args
        and isinstance(args["body"], dict)
        and "action" in args["body"]
    ):
        args = args["body"]
    return args


_active_document_id = None
_active_model = None


def set_active_document(doc_id):
    global _active_document_id
    _active_document_id = doc_id


def set_active_model(model):
    global _active_model
    _active_model = model


def get_active_document():
    return _active_document_id


def clear_active_document(doc_id=None):
    global _active_document_id
    if doc_id is None or _active_document_id == doc_id:
        _active_document_id = None
        return True
    return False


def _owned_document_query(query, Document, owner):
    if owner is None:
        from sqlalchemy import false
        return query.filter(false())
    return query.filter(Document.owner == owner)


def _get_owned_document(db, Document, doc_id, owner, active_only=False):
    q = db.query(Document).filter(Document.id == doc_id)
    if active_only:
        q = q.filter(Document.is_active == True)
    q = _owned_document_query(q, Document, owner)
    return q.first()


def _most_recent_owned_document(db, Document, owner, active_only=False):
    q = db.query(Document)
    if active_only:
        q = q.filter(Document.is_active == True)
    q = _owned_document_query(q, Document, owner)
    return q.order_by(Document.updated_at.desc()).first()


_INTERNAL_BASE = internal_api_base()


from . import (  # noqa: E402, F401
    documents,
    chats,
    skills,
    tasks,
    models,
    integrations,
    settings as settings_mod,
    api,
    notes,
    calendar,
    app_api,
    images,
    research,
    contacts,
    vault,
)

from .documents import (  # noqa: E402, F401
    _sniff_doc_language,
    _looks_like_email_document,
    _coerce_email_document_content,
    do_create_document,
    do_update_document,
    do_edit_document,
    do_suggest_document,
    do_manage_documents,
)
from .chats import do_search_chats  # noqa: E402, F401
from .skills import (  # noqa: E402, F401
    _skill_dump,
    do_manage_skills,
)
from .tasks import do_manage_tasks  # noqa: E402, F401
from .models import (  # noqa: E402, F401
    do_manage_endpoints,
    do_download_model,
    do_serve_model,
    do_list_served_models,
    do_stop_served_model,
    do_tail_serve_output,
    do_list_downloads,
    do_cancel_download,
    do_search_hf_models,
    do_adopt_served_model,
    do_list_cookbook_servers,
    do_list_serve_presets,
    do_serve_preset,
    do_list_cached_models,
    _cookbook_apply_retry_suggestion,
    _scan_running_model_processes,
)
from .integrations import (  # noqa: E402, F401
    do_manage_mcp,
    do_manage_webhooks,
    do_manage_tokens,
)
from .settings import do_manage_settings  # noqa: E402, F401
from .api import do_api_call  # noqa: E402, F401
from .notes import do_manage_notes  # noqa: E402, F401
from .calendar import do_manage_calendar  # noqa: E402, F401
from .app_api import (  # noqa: E402, F401
    do_app_api,
    _internal_headers,
)
from .images import do_edit_image  # noqa: E402, F401
from .research import (  # noqa: E402, F401
    do_manage_research,
    do_trigger_research,
)
from .contacts import (  # noqa: E402, F401
    do_resolve_contact,
    do_manage_contact,
)
from .vault import (  # noqa: E402, F401
    do_vault_search,
    do_vault_get,
    do_vault_unlock,
    _load_vault_config,
)
