"""MeshCore dashboard API — exposes node status, contacts, and stats.

Routes are mounted at /api/plugins/meshcore-platform/ by the dashboard.
Reads from the shared state file written by the gateway adapter's keepalive loop.
"""

import json
import os
import time
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

STATE_FILE = "/tmp/hermes-meshcore-state.json"
MAX_STALE_SECONDS = 60  # Consider data stale if older than 60s


def _read_state() -> dict:
    """Read the shared state file written by the gateway adapter."""
    try:
        if not os.path.exists(STATE_FILE):
            return {"connected": False, "error": "Gateway not running (no state file)"}
        with open(STATE_FILE) as f:
            state = json.load(f)
        # Check staleness
        age = time.time() - state.get("updated_at", 0)
        if age > MAX_STALE_SECONDS:
            state["connected"] = False
            state["stale"] = True
            state["stale_seconds"] = round(age, 1)
        return state
    except Exception as e:
        return {"connected": False, "error": str(e)}


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status():
    """Full node status — connection, radio, stats, contacts."""
    return JSONResponse(_read_state())


@router.get("/contacts")
async def get_contacts():
    """List all known contacts with basic info."""
    state = _read_state()
    if not state.get("connected"):
        return JSONResponse({"error": state.get("error", "Gateway not running")}, status_code=503)

    # For detailed contacts, we need the adapter — fall back to summary from state file
    contacts = state.get("contacts", {})
    return JSONResponse({
        "contacts": [],  # Detailed list requires adapter access; use /status for summary
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
