"""Agent tools for MeshCore.

Declared in ``plugin.yaml`` under ``provides_tools`` so Hermes registers them
in every process (CLI and TUI included), not only where the gateway adapter
is materialised. Each handler uses the live adapter when this process runs
the profile's gateway, and the profile-scoped IPC files otherwise.

Kept import-light: no gateway imports at module level.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from . import registry, repeater, state

CONTACT_SCHEMA = {
    "name": "meshcore_contact",
    "description": (
        "Look up a MeshCore node in the local contact cache by name or pubkey prefix. Returns its "
        "full pubkey, type (client/repeater/room/sensor), hop count, location and last advert time. "
        "No radio traffic."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Node name (exact or unique part) or pubkey prefix"},
        },
        "required": ["name"],
    },
}

ADMIN_SCHEMA = {
    "name": "meshcore_admin",
    "description": (
        "Query a remote MeshCore repeater or room server over the mesh (read-only). Commands: "
        + "; ".join(f"{name}: {cmd.description}" for name, cmd in repeater.COMMANDS.items())
        + "; all: status, neighbours, owner and clock in one go. Each query is a LoRa round trip "
        "and can take 5-60 seconds, so do not repeat it needlessly. Binary queries log in first "
        "(guest login with no password works on most repeaters)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "Repeater name or pubkey prefix"},
            "command": {"type": "string", "enum": list(repeater.COMMANDS) + ["all"]},
            "password": {
                "type": "string",
                "description": "Optional repeater password (only usable inside the gateway process; never stored)",
            },
        },
        "required": ["node", "command"],
    },
}


def _contact_from_state(query: str) -> Dict[str, Any]:
    snap = state.read_state()
    contacts = state.list_contacts(snap)
    if not contacts:
        return {"success": False, "error": snap.get("error") or "No MeshCore contacts known (is the gateway running?)"}
    q = query.strip().lower()
    if not q:
        return {"success": False, "error": "No node name or pubkey prefix given"}
    exact = [c for c in contacts if c.get("name", "").lower() == q]
    by_key = [c for c in contacts if len(q) >= 4 and c.get("pubkey", "").startswith(q)]
    partial = [c for c in contacts if q in c.get("name", "").lower()]
    for group in (exact, by_key, partial):
        if len(group) == 1:
            result = dict(group[0], success=True)
            if not snap.get("fresh"):
                result["note"] = "from a stale snapshot; the gateway may be down"
            return result
    if partial or by_key:
        names = ", ".join(sorted(c["name"] for c in (partial or by_key))[:8])
        return {"success": False, "error": f"'{query}' matches several nodes: {names}"}
    return {"success": False, "error": f"No known node matches '{query}'"}


async def handle_contact(args: Dict[str, Any], **_kw) -> str:
    name = str(args.get("name", ""))
    adapter = registry.current()
    result = adapter.contact_details(name) if adapter is not None else _contact_from_state(name)
    return json.dumps(result)


async def handle_admin(args: Dict[str, Any], **_kw) -> str:
    node = str(args.get("node", ""))
    command = str(args.get("command", ""))
    password = str(args.get("password", "") or "")
    if repeater.resolve_command(command) is None:
        return json.dumps({"success": False,
                           "error": f"Unknown command {command!r}. Supported: {repeater.supported_commands_text()}"})
    adapter = registry.current()
    if adapter is not None:
        return json.dumps(await adapter.query_remote(node, command, password))
    if password:
        return json.dumps({"success": False, "error": (
            "Passwords are only accepted inside the gateway process (they are never written to disk). "
            "Ask from a MeshCore admin DM, or omit the password for guest access.")})
    timeout = 240 if repeater.resolve_command(command) == "all" else 90
    return json.dumps(await state.call_gateway("admin_query", {"node": node, "command": command}, timeout=timeout))


def admin_available() -> bool:
    adapter = registry.current()
    if adapter is not None:
        return adapter.settings.admin_enabled
    snap = state.read_state()
    return bool(snap.get("fresh") and (snap.get("config") or {}).get("admin_enabled"))


def register_tools(ctx) -> None:
    ctx.register_tool(name="meshcore_contact", toolset="meshcore", schema=CONTACT_SCHEMA,
                      handler=handle_contact, is_async=True, emoji="📇",
                      description=CONTACT_SCHEMA["description"])
    ctx.register_tool(name="meshcore_admin", toolset="meshcore_admin", schema=ADMIN_SCHEMA,
                      handler=handle_admin, is_async=True, emoji="🛰️", check_fn=admin_available,
                      description=ADMIN_SCHEMA["description"])
