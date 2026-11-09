"""MeshCore dashboard backend, mounted at ``/api/plugins/meshcore-platform/``.

Hermes imports this file on its own (not as part of the plugin package), so
the shared ``state`` helpers are loaded by file path. Every request resolves
the profile through ``hermes_constants.get_hermes_home()``, which the
dashboard scopes per request on a multi-profile install.

Handlers never block the event loop: file I/O is tiny and the gateway restart
runs as a detached subprocess.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _load_state_module():
    name = "hermes_meshcore_dashboard_state"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_ROOT / "state.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


state = _load_state_module()
router = APIRouter()

_HEX_LIST = re.compile(r"^(?:[0-9a-fA-F]{8,64})(?:,[0-9a-fA-F]{8,64})*$")
_INT_LIST = re.compile(r"^(?:\d{1,3})(?:,\d{1,3})*$")
_CHAT_LIST = re.compile(r"^(?:channel:\d{1,3})(?:,channel:\d{1,3})*$")
_BOOL = {"true", "false"}

# dashboard key -> (env var, validator, normaliser)
CONFIG_FIELDS: Dict[str, tuple] = {
    "admin_nodes": ("MESHCORE_ADMIN_NODES", _HEX_LIST),
    "allowed_users": ("MESHCORE_ALLOWED_USERS", _HEX_LIST),
    "admin_channels": ("MESHCORE_ADMIN_CHANNELS", _INT_LIST),
    "monitor_channels": ("MESHCORE_MONITOR_CHANNELS", _INT_LIST),
    "group_allowed_chats": ("MESHCORE_GROUP_ALLOWED_CHATS", _CHAT_LIST),
    "require_mention": ("MESHCORE_REQUIRE_MENTION", re.compile(r"^(?:true|(?:\d{1,3})(?:,\d{1,3})*)$")),
    "enable_dms": ("MESHCORE_ENABLE_DMS", _BOOL),
    "allow_all_users": ("MESHCORE_ALLOW_ALL_USERS", _BOOL),
}


def _env_path() -> Path:
    return state.hermes_home() / ".env"


def _read_env() -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        lines = _env_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            if key.startswith("export "):
                key = key[7:]
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def _validate(key: str, value: Any) -> str:
    env, rule = CONFIG_FIELDS[key]
    if isinstance(value, bool):
        value = "true" if value else "false"
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    text = re.sub(r"\s+", "", str(value if value is not None else ""))
    if text == "":
        return ""
    text = text.lower()
    ok = text in rule if isinstance(rule, set) else bool(rule.match(text))
    if not ok:
        raise HTTPException(status_code=400, detail=f"Invalid value for {key}")
    return text


def _write_env(updates: Dict[str, str]) -> None:
    path = _env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        lines = []
    pending = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if stripped and not stripped.startswith("#") and key in pending:
            value = pending.pop(key)
            if value:
                out.append(f"{key}={value}\n")
            continue
        out.append(line if line.endswith("\n") else line + "\n")
    out.extend(f"{key}={value}\n" for key, value in pending.items() if value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _current_config() -> Dict[str, str]:
    env = _read_env()
    return {key: env.get(var, "") for key, (var, _rule) in CONFIG_FIELDS.items()}


# ── status ───────────────────────────────────────────────────────────────

def _public_state() -> Dict[str, Any]:
    snap = state.read_state()
    snap.pop("contacts_list", None)
    return snap


@router.get("/status")
async def get_status():
    return JSONResponse(_public_state())


@router.get("/health")
async def get_health():
    snap = state.read_state()
    stats = snap.get("stats") or {}
    return JSONResponse({
        "connected": bool(snap.get("connected")),
        "fresh": bool(snap.get("fresh")),
        "battery_mv": stats.get("battery_mv"),
        "last_message_at": snap.get("last_message_at"),
        "contact_count": (snap.get("contacts") or {}).get("total", 0),
    })


@router.get("/contacts")
async def get_contacts():
    snap = state.read_state()
    if not snap.get("connected"):
        return JSONResponse({"error": snap.get("error") or "Gateway not connected", "contacts": []}, status_code=503)
    return JSONResponse({"contacts": state.list_contacts(snap), **(snap.get("contacts") or {})})


@router.get("/nodes")
async def get_nodes():
    snap = state.read_state()
    nodes = [c for c in state.list_contacts(snap) if c.get("type") in ("repeater", "room")]
    return JSONResponse({"nodes": nodes, "commands": snap.get("commands") or {}})


# ── config ───────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config():
    return JSONResponse(_current_config())


@router.post("/config")
async def update_config(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    unknown = sorted(set(body) - set(CONFIG_FIELDS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown config keys: {', '.join(unknown)}")
    updates = {CONFIG_FIELDS[key][0]: _validate(key, value) for key, value in body.items()}
    if not updates:
        raise HTTPException(status_code=400, detail="No config keys provided")
    await asyncio.to_thread(_write_env, updates)
    return JSONResponse({"success": True, "config": _current_config(), "restart_required": True})


@router.post("/restart")
async def restart_gateway():
    """Ask Hermes to restart the gateway. Detached: the gateway may host this very dashboard."""
    env = dict(os.environ, HERMES_HOME=str(state.hermes_home()))
    try:
        await asyncio.create_subprocess_exec(
            "hermes", "gateway", "restart",
            env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError:
        return JSONResponse({"success": False, "error": "hermes CLI not found on PATH"}, status_code=500)
    return JSONResponse({"success": True, "message": "Gateway restart requested"})


# ── remote admin ─────────────────────────────────────────────────────────

@router.post("/admin/query")
async def submit_admin_query(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    node = str(body.get("node", "")).strip()
    command = str(body.get("command", "")).strip()
    if not node or not command:
        raise HTTPException(status_code=400, detail="node and command are required")
    if body.get("password"):
        raise HTTPException(status_code=400, detail=(
            "Passwords are not accepted from the dashboard (they would have to be written to disk). "
            "Use guest access, or ask the agent from an admin DM."))
    snap = state.read_state()
    if not snap.get("fresh"):
        raise HTTPException(status_code=503, detail=snap.get("error") or "Gateway not running")
    if not (snap.get("config") or {}).get("admin_enabled"):
        raise HTTPException(status_code=403, detail="Remote admin is disabled (set MESHCORE_ADMIN_NODES)")
    request_id = state.submit_request("admin_query", {"node": node, "command": command})
    return JSONResponse({"success": True, "request_id": request_id})


@router.get("/admin/result")
async def get_admin_result(request_id: str = ""):
    try:
        result = state.read_response(request_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request id")
    if result is not None:
        return JSONResponse({"status": "complete", "result": result})
    queued = state.request_pending(request_id)
    return JSONResponse({"status": "pending", "queued": queued})


@router.post("/advert")
async def trigger_advert():
    snap = state.read_state()
    if not snap.get("fresh"):
        raise HTTPException(status_code=503, detail=snap.get("error") or "Gateway not running")
    request_id = state.submit_request("advert", {"flood": True})
    return JSONResponse({"success": True, "request_id": request_id})
