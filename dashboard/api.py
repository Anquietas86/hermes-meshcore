"""MeshCore dashboard API — node status, contacts, stats, and configuration.

Routes are mounted at /api/plugins/meshcore-platform/ by the dashboard.
Reads from the shared state file written by the gateway adapter's keepalive loop.
Config reads/writes the meshcore profile's config.yaml.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter()

STATE_FILE = "/tmp/hermes-meshcore-state.json"
MAX_STALE_SECONDS = 60

# Auto-detect which profile runs MeshCore — check meshcore profile first, fall back to default
def _detect_profile() -> str:
    """Return the profile name that runs MeshCore (meshcore or default)."""
    meshcore_cfg = os.path.expanduser("~/.hermes/profiles/meshcore/config.yaml")
    if os.path.exists(meshcore_cfg):
        return "meshcore"
    return "default"

def _config_path() -> str:
    profile = _detect_profile()
    if profile == "default":
        return os.path.expanduser("~/.hermes/config.yaml")
    return os.path.expanduser(f"~/.hermes/profiles/{profile}/config.yaml")

def _env_path() -> str:
    profile = _detect_profile()
    if profile == "default":
        return os.path.expanduser("~/.hermes/.env")
    return os.path.expanduser(f"~/.hermes/profiles/{profile}/.env")

# ── Config keys we expose for editing ──────────────────────────────────────
CONFIG_KEYS = [
    "admin_nodes",
    "admin_channels",
    "monitor_channels",
    "require_mention_channels",
    "allow_all_users",
    "allowed_users",
    "enable_dms",
]


def _read_state() -> dict:
    """Read the shared state file written by the gateway adapter."""
    try:
        if not os.path.exists(STATE_FILE):
            return {"connected": False, "error": "Gateway not running (no state file)"}
        with open(STATE_FILE) as f:
            state = json.load(f)
        age = time.time() - state.get("updated_at", 0)
        if age > MAX_STALE_SECONDS:
            state["connected"] = False
            state["stale"] = True
            state["stale_seconds"] = round(age, 1)
        return state
    except Exception as e:
        return {"connected": False, "error": str(e)}


def _read_config() -> dict:
    """Read meshcore platform config — .env first (what gateway uses), then config.yaml extra."""
    try:
        # Read .env vars (what the gateway actually uses)
        env_path = _env_path()
        env_vars = {}
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        env_vars[key.strip()] = val.strip().strip('"').strip("'")

        # Read config.yaml extra (fallback)
        with open(_config_path()) as f:
            cfg = yaml.safe_load(f) or {}
        extra = (
            cfg.get("platforms", {})
            .get("meshcore", {})
            .get("extra", {})
        )

        # .env takes priority (matches gateway behaviour)
        return {
            "admin_nodes": env_vars.get("MESHCORE_ADMIN_NODES", extra.get("admin_nodes", "")),
            "admin_channels": env_vars.get("MESHCORE_ADMIN_CHANNELS", extra.get("admin_channels", "")),
            "monitor_channels": env_vars.get("MESHCORE_MONITOR_CHANNELS", extra.get("monitor_channels", "")),
            "require_mention_channels": env_vars.get("MESHCORE_REQUIRE_MENTION", extra.get("require_mention_channels", "")),
            "allow_all_users": env_vars.get("MESHCORE_ALLOW_ALL_USERS", extra.get("allow_all_users", "true")),
            "allowed_users": env_vars.get("MESHCORE_ALLOWED_USERS", extra.get("allowed_users", "")),
            "enable_dms": env_vars.get("MESHCORE_ENABLE_DMS", extra.get("enable_dms", "true")),
        }
    except Exception as e:
        return {"error": str(e)}


# Map config keys to .env variable names
CONFIG_TO_ENV = {
    "admin_nodes": "MESHCORE_ADMIN_NODES",
    "admin_channels": "MESHCORE_ADMIN_CHANNELS",
    "monitor_channels": "MESHCORE_MONITOR_CHANNELS",
    "require_mention_channels": "MESHCORE_REQUIRE_MENTION",
    "allow_all_users": "MESHCORE_ALLOW_ALL_USERS",
    "allowed_users": "MESHCORE_ALLOWED_USERS",
    "enable_dms": "MESHCORE_ENABLE_DMS",
}


def _write_config(updates: dict) -> dict:
    """Write meshcore platform config to both .env (what gateway reads) and config.yaml extra."""
    try:
        # Write to config.yaml extra
        with open(_config_path()) as f:
            cfg = yaml.safe_load(f) or {}
        cfg.setdefault("platforms", {}).setdefault("meshcore", {}).setdefault("extra", {})
        extra = cfg["platforms"]["meshcore"]["extra"]
        for key in CONFIG_KEYS:
            if key in updates:
                extra[key] = str(updates[key])
        with open(_config_path(), "w") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # Write to .env (what the gateway actually reads)
        env_path = _env_path()
        if os.path.exists(env_path):
            with open(env_path) as f:
                env_lines = f.readlines()
            new_lines = []
            updated_keys = set()
            for line in env_lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key, _, _ = stripped.partition("=")
                    key = key.strip()
                    if key in CONFIG_TO_ENV.values():
                        config_key = [k for k, v in CONFIG_TO_ENV.items() if v == key][0]
                        if config_key in updates:
                            new_lines.append(f"{key}={updates[config_key]}\n")
                            updated_keys.add(key)
                            continue
                new_lines.append(line)
            # Add any new keys not already in .env
            for config_key, env_key in CONFIG_TO_ENV.items():
                if config_key in updates and env_key not in updated_keys:
                    new_lines.append(f"{env_key}={updates[config_key]}\n")
            with open(env_path, "w") as f:
                f.writelines(new_lines)

        return _read_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {e}")


def _restart_gateway() -> dict:
    """Trigger a gateway restart via hermes CLI."""
    try:
        result = subprocess.run(
            ["hermes", "--profile", _detect_profile(), "gateway", "restart"],
            capture_output=True, text=True, timeout=30,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Restart timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Status routes ──────────────────────────────────────────────────────────

@router.get("/status")
async def get_status():
    """Full node status — connection, radio, stats, contacts, admin."""
    return JSONResponse(_read_state())


@router.get("/contacts")
async def get_contacts():
    """List all known contacts with basic info."""
    state = _read_state()
    if not state.get("connected"):
        return JSONResponse({"error": state.get("error", "Gateway not running")}, status_code=503)
    contacts = state.get("contacts", {})
    return JSONResponse({
        "contacts": [],
        "total": contacts.get("total", 0),
        "repeaters": contacts.get("repeaters", 0),
        "clients": contacts.get("clients", 0),
        "rooms": contacts.get("rooms", 0),
    })


@router.get("/nodes")
async def get_nodes():
    """List known repeater nodes for the admin query dropdown."""
    state = _read_state()
    if not state.get("connected"):
        return JSONResponse({"error": state.get("error", "Gateway not running")}, status_code=503)
    return JSONResponse({"nodes": state.get("known_nodes", [])})


@router.get("/health")
async def get_health():
    """Quick health check — connected, battery, last message."""
    state = _read_state()
    stats = state.get("stats", {})
    return JSONResponse({
        "connected": state.get("connected", False),
        "battery_mv": stats.get("battery_mv"),
        "uptime_s": stats.get("uptime_s"),
        "last_message_ago_s": state.get("last_message_ago_s"),
        "contact_count": state.get("contacts", {}).get("total", 0),
    })


# ── Config routes ──────────────────────────────────────────────────────────

@router.get("/config")
async def get_config():
    """Read current meshcore platform configuration."""
    return JSONResponse(_read_config())


@router.post("/config")
async def update_config(request: Request):
    """Update meshcore platform configuration. Accepts any subset of config keys."""
    raw = await request.body()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    updates = {k: v for k, v in body.items() if k in CONFIG_KEYS}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid config keys provided")
    result = _write_config(updates)
    return JSONResponse({"success": True, "config": result})


@router.post("/restart")
async def restart_gateway():
    """Restart the meshcore gateway to apply config changes."""
    result = _restart_gateway()
    return JSONResponse(result)


# ── Admin query routes ──────────────────────────────────────────────────────

ADMIN_REQUEST_FILE = "/tmp/hermes-meshcore-admin-request.json"
ADMIN_RESPONSE_FILE = "/tmp/hermes-meshcore-admin-response.json"


@router.post("/admin/query")
async def submit_admin_query(request: Request):
    """Submit an admin query for a remote repeater. The gateway's keepalive
    loop picks it up within 15s and writes the response file."""
    raw = await request.body()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    node = body.get("node", "").strip()
    command = body.get("command", "").strip()
    password = body.get("password", "")

    if not node or not command:
        raise HTTPException(status_code=400, detail="node and command are required")

    # Check if a request is already pending
    if os.path.exists(ADMIN_REQUEST_FILE):
        raise HTTPException(status_code=409, detail="An admin query is already in progress")

    request_id = str(int(time.time()))
    req_data = {
        "request_id": request_id,
        "node": node,
        "command": command,
        "password": password,
        "submitted_at": time.time(),
    }
    with open(ADMIN_REQUEST_FILE, "w") as f:
        json.dump(req_data, f)

    return JSONResponse({"success": True, "request_id": request_id, "message": "Query submitted — gateway will process within 15s"})


@router.get("/admin/result")
async def get_admin_result(request_id: str = ""):
    """Poll for the result of an admin query. Returns the response if complete,
    or status=pending if still waiting."""
    if os.path.exists(ADMIN_REQUEST_FILE):
        return JSONResponse({"status": "pending", "message": "Request not yet picked up by gateway"})

    if not os.path.exists(ADMIN_RESPONSE_FILE):
        return JSONResponse({"status": "pending", "message": "Waiting for response…"})

    with open(ADMIN_RESPONSE_FILE) as f:
        result = json.load(f)

    # If request_id provided, only return matching result
    if request_id and result.get("request_id") != request_id:
        return JSONResponse({"status": "pending", "message": "Different request in progress"})

    # Clean up response file after reading
    os.remove(ADMIN_RESPONSE_FILE)
    return JSONResponse({"status": "complete", "result": result})
