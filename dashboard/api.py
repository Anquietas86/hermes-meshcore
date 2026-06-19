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
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

STATE_FILE = "/tmp/hermes-meshcore-state.json"
MAX_STALE_SECONDS = 60
CONFIG_PATH = os.path.expanduser("~/.hermes/profiles/meshcore/config.yaml")

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
    """Read meshcore platform extra config from config.yaml."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        extra = (
            cfg.get("platforms", {})
            .get("meshcore", {})
            .get("extra", {})
        )
        return {
            "admin_nodes": extra.get("admin_nodes", ""),
            "admin_channels": extra.get("admin_channels", ""),
            "monitor_channels": extra.get("monitor_channels", ""),
            "require_mention_channels": extra.get("require_mention_channels", ""),
            "allow_all_users": extra.get("allow_all_users", "true"),
            "allowed_users": extra.get("allowed_users", ""),
            "enable_dms": extra.get("enable_dms", "true"),
        }
    except Exception as e:
        return {"error": str(e)}


def _write_config(updates: dict) -> dict:
    """Write meshcore platform extra config to config.yaml. Returns updated values."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}

        # Ensure nested structure exists
        cfg.setdefault("platforms", {}).setdefault("meshcore", {}).setdefault("extra", {})

        extra = cfg["platforms"]["meshcore"]["extra"]
        for key in CONFIG_KEYS:
            if key in updates:
                extra[key] = str(updates[key])

        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        return _read_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {e}")


def _restart_gateway() -> dict:
    """Trigger a gateway restart via hermes CLI."""
    try:
        result = subprocess.run(
            ["hermes", "--profile", "meshcore", "gateway", "restart"],
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
async def update_config(body: dict):
    """Update meshcore platform configuration. Accepts any subset of config keys."""
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
