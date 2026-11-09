"""Which tools non-admin MeshCore senders may use, and enforcement of that.

Two layers:

1. ``toolsets_for_source`` (in the adapter) tells Hermes which toolsets a
   public sender gets. Hermes adds every enabled MCP server and every plugin
   toolset to such a list unless told otherwise, so that list alone is not a
   boundary.
2. A ``pre_tool_call`` hook vetoes any tool call in a non-admin MeshCore
   session whose tool is not on the public allowlist. This holds whatever
   Hermes enabled.

The operator picks extra individual tools (``MESHCORE_PUBLIC_TOOLS``) from a
catalog of what the gateway can offer, which the dashboard shows. Hermes
internals used here (tool registry, MCP read-only hints, runtime toolsets)
are all optional: on any failure we fall back to whole toolsets plus the hook.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

PLATFORM = "meshcore"
PUBLIC_TOOLSET = "meshcore_public"
ADMIN_ONLY_TOOLS = frozenset({"meshcore_admin"})
# Deferred tools are invisible without discovery; these only read metadata, so they are
# always allowed for public senders and are not operator-managed (they are not in the
# toolset registry, so the dashboard picker cannot list them).
DISCOVERY_TOOLS = frozenset({"tool_search", "tool_describe"})
# Built-in tools known to only read. Everything else is "not marked read-only".
KNOWN_READ_ONLY = frozenset({"web_search", "web_extract", "x_search", "vision_analyze",
                             "meshcore_contact", "meshcore_nearby"})
# Used when Hermes can't resolve toolsets for us.
FALLBACK_TOOLSET_TOOLS = {"meshcore": {"meshcore_contact", "meshcore_nearby"},
                          "web": {"web_search", "web_extract"}, "search": {"web_search"}}
BLOCK_MESSAGE = ("This tool is not available to public MeshCore senders. Answer from what you can look up "
                 "with the tools you have, or say you can't help with that here.")


# ── Hermes internals (all optional) ─────────────────────────────────────

def _registry():
    try:
        from tools.registry import registry  # Hermes's tool registry, not this plugin's tools.py
        return registry
    except Exception:
        return None


def mcp_server_names() -> Set[str]:
    try:
        from tools import mcp_tool as core
        return {str(s) for s in core._mcp_tool_server_names.values()}
    except Exception:
        return set()


def _mcp_read_only(tool_name: str) -> Optional[bool]:
    """The MCP server's own readOnlyHint for a registered tool; None when unknown."""
    try:
        from tools import mcp_tool as core
        from tools.mcp_tool_schema import mcp_prefixed_tool_name
        from tools.mcp_tool_scope import _resolve_server_key
        server = core._mcp_tool_server_names.get(tool_name)
        if not server:
            return None
        hints = core._tool_read_only_hints.get(_resolve_server_key(server), {})
        for original, read_only in hints.items():
            if mcp_prefixed_tool_name(server, original) == tool_name:
                return bool(read_only)
    except Exception:
        pass
    return None


def resolve_toolset_tools(name: str) -> Set[str]:
    # Our own toolset's tools are always known, even before (or without) Hermes's registry listing them.
    own = set(FALLBACK_TOOLSET_TOOLS["meshcore"]) if name == "meshcore" else set()
    try:
        from toolsets import resolve_toolset
        return set(resolve_toolset(name)) | own
    except Exception:
        return set(FALLBACK_TOOLSET_TOOLS.get(name, ())) | own


def catalog(limit_description: int = 160) -> List[Dict[str, Any]]:
    """Every tool the gateway can offer, for the dashboard's public-access picker."""
    reg = _registry()
    if reg is None:
        return []
    try:
        entries = list(reg.get_all_entries())
    except Exception:
        logger.debug("MeshCore: tool registry unavailable", exc_info=True)
        return []
    servers = {}
    try:
        from tools import mcp_tool as core
        servers = dict(core._mcp_tool_server_names)
    except Exception:
        pass
    out = []
    for entry in entries:
        name = getattr(entry, "name", "")
        if not name or name in ADMIN_ONLY_TOOLS:
            continue
        read_only = _mcp_read_only(name) if name in servers else (True if name in KNOWN_READ_ONLY else None)
        out.append({
            "name": name,
            "toolset": str(getattr(entry, "toolset", "") or ""),
            "server": servers.get(name),
            "description": " ".join(str(getattr(entry, "description", "") or "").split())[:limit_description],
            "read_only": read_only,
        })
    out.sort(key=lambda t: (t["toolset"], t["name"]))
    return out


def ensure_public_toolset(tools: Iterable[str]) -> bool:
    """Register (or refresh) the runtime toolset holding the operator's chosen tools."""
    chosen = sorted(set(tools) - ADMIN_ONLY_TOOLS)
    if not chosen:
        return False
    try:
        from toolsets import create_custom_toolset
        create_custom_toolset(PUBLIC_TOOLSET, "Tools the operator opened to public MeshCore senders",
                              tools=chosen)
        return True
    except Exception:
        logger.debug("MeshCore: could not register the %s toolset", PUBLIC_TOOLSET, exc_info=True)
        return False


# ── Policy ──────────────────────────────────────────────────────────────

def names_mcp_server(toolsets: Iterable[str], servers: Optional[Set[str]] = None) -> bool:
    servers = mcp_server_names() if servers is None else servers
    return any(t.startswith("mcp-") or t in servers for t in toolsets)


def mcp_servers_for(tools: Iterable[str]) -> List[str]:
    """The ``mcp-<server>`` toolsets behind the chosen MCP tools."""
    try:
        from tools import mcp_tool as core
        owners = core._mcp_tool_server_names
    except Exception:
        return []
    return list(dict.fromkeys(f"mcp-{owners[t]}" for t in tools if owners.get(t)))


def public_toolset_list(public_toolsets: Iterable[str], public_tools: Iterable[str],
                        servers: Optional[Set[str]] = None) -> List[str]:
    """What ``toolsets_for_source`` returns for a non-admin sender."""
    public_tools = list(public_tools)
    names = [t for t in dict.fromkeys(public_toolsets) if t and t not in ADMIN_ONLY_TOOLS]
    if public_tools and ensure_public_toolset(public_tools):
        names.append(PUBLIC_TOOLSET)
    # MCP tools only exist in a session whose list names their server. Name just those servers;
    # the pre_tool_call hook blocks that server's other tools.
    names += mcp_servers_for(public_tools)
    names = list(dict.fromkeys(names)) or ["meshcore"]
    # Without a named MCP server Hermes enables every MCP server for this list. "no_mcp" stops that.
    if not names_mcp_server(names, servers):
        names.append("no_mcp")
    return names


def allowed_public_tools(public_toolsets: Iterable[str], public_tools: Iterable[str]) -> Set[str]:
    allowed: Set[str] = set(public_tools) | DISCOVERY_TOOLS
    for name in public_toolsets:
        allowed |= resolve_toolset_tools(name)
    return allowed - ADMIN_ONLY_TOOLS


# ── pre_tool_call hook ──────────────────────────────────────────────────

def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, "") or "")
    except Exception:
        return ""


class _Source:
    def __init__(self, chat_id: str, chat_type: str, user_id: str) -> None:
        self.chat_id, self.chat_type, self.user_id = chat_id, chat_type, user_id


def _live_adapter():
    from . import registry
    adapter = registry.current()
    if adapter is None and len(registry._LIVE) == 1:
        adapter = next(iter(registry._LIVE.values()))
    return adapter


def pre_tool_call(tool_name: str = "", args: Any = None, **_kw) -> Optional[Dict[str, str]]:
    """Veto tools outside the public allowlist in non-admin MeshCore sessions."""
    try:
        if _session("HERMES_SESSION_PLATFORM").lower() != PLATFORM:
            return None
        chat_type = _session("HERMES_SESSION_CHAT_TYPE").lower()
        if "." in chat_type:  # str(ChatType.DM) on enum-based cores
            chat_type = chat_type.rsplit(".", 1)[-1]
        source = _Source(_session("HERMES_SESSION_CHAT_ID"), chat_type, _session("HERMES_SESSION_USER_ID"))
        adapter = _live_adapter()
        if adapter is None:
            return {"action": "block", "message": BLOCK_MESSAGE}
        if adapter.is_admin_source(source):
            return None
        if tool_name in adapter.public_tool_allowlist():
            return None
        logger.info("MeshCore: blocked %s for public sender in %s", tool_name, source.chat_id)
        return {"action": "block", "message": BLOCK_MESSAGE}
    except Exception:
        logger.warning("MeshCore: tool policy check failed; blocking %s", tool_name, exc_info=True)
        return {"action": "block", "message": BLOCK_MESSAGE}
