"""Stand-in for Hermes's toolsets module."""

TOOLSETS = {
    "meshcore": {"tools": ["meshcore_contact", "meshcore_nearby"], "includes": []},
    "web": {"tools": ["web_search", "web_extract"], "includes": []},
    "terminal": {"tools": ["terminal", "process_manage"], "includes": []},
}


def create_custom_toolset(name, description, tools=None, includes=None):
    TOOLSETS[name] = {"description": description, "tools": list(tools or []), "includes": list(includes or [])}


def resolve_toolset(name, visited=None):
    spec = TOOLSETS.get(name) or {}
    out = set(spec.get("tools", []))
    for inc in spec.get("includes", []):
        out |= set(resolve_toolset(inc))
    return sorted(out)
