"""MeshCore dashboard API — exposes node status, contacts, and stats.

Routes are mounted at /api/plugins/meshcore-platform/ by the dashboard.
Reads from the gateway adapter's live state (contacts, self_info, stats cache).
"""

import json
import os
import time
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

# ── Adapter access ─────────────────────────────────────────────────────────

def _get_adapter():
    """Get the live MeshCoreAdapter instance from the running gateway."""
    try:
        from hermes_plugins.meshcore_platform.adapter import MeshCoreAdapter
        return MeshCoreAdapter._instance
    except ImportError:
        return None


def _adapter_state(adapter) -> dict:
    """Extract live state from the adapter for dashboard display."""
    if adapter is None:
        return {"connected": False, "error": "Gateway not running"}

    conn = adapter._conn
    connected = conn is not None and conn.is_connected

    # Self info
    self_info = adapter._self_info or {}
    node_name = self_info.get("name", "unknown")
    pubkey = self_info.get("public_key", "")
    lat = self_info.get("adv_lat")
    lon = self_info.get("adv_lon")
    radio_freq = self_info.get("radio_freq")
    radio_bw = self_info.get("radio_bw")
    radio_sf = self_info.get("radio_sf")
    radio_cr = self_info.get("radio_cr")

    # Stats cache
    stats = adapter._stats_cache or {}
    battery = stats.get("battery")
    uptime = stats.get("uptime")
    tx_packets = stats.get("tx_packets")
    rx_packets = stats.get("rx_packets")
    noise = stats.get("noise")
    rssi = stats.get("rssi")
    snr = stats.get("snr")

    # Contacts
    contacts = adapter._contacts or {}
    contact_count = len(contacts)
    repeater_count = sum(1 for c in contacts.values() if c.get("type") == 2)
    client_count = sum(1 for c in contacts.values() if c.get("type") == 1)
    room_count = sum(1 for c in contacts.values() if c.get("type") == 3)

    # Channels
    channels = sorted(adapter._discovered_channels) if adapter._discovered_channels else []

    # Last message time
    last_msg = adapter._last_message_time
    last_msg_ago = time.time() - last_msg if last_msg else None

    return {
        "connected": connected,
        "host": adapter.host,
        "port": adapter.port,
        "node": {
            "name": node_name,
            "pubkey_prefix": pubkey[:12] if len(pubkey) >= 12 else "",
            "lat": lat,
            "lon": lon,
            "radio": {
                "freq_mhz": round(radio_freq, 3) if radio_freq else None,
                "bw_khz": round(radio_bw, 1) if radio_bw else None,
                "sf": radio_sf,
                "cr": radio_cr,
            } if any([radio_freq, radio_bw, radio_sf, radio_cr]) else None,
        },
        "stats": {
            "battery_mv": battery,
            "uptime_s": uptime,
            "tx_packets": tx_packets,
            "rx_packets": rx_packets,
            "noise": noise,
            "rssi": rssi,
            "snr": snr,
        },
        "contacts": {
            "total": contact_count,
            "repeaters": repeater_count,
            "clients": client_count,
            "rooms": room_count,
        },
        "channels": channels,
        "last_message_ago_s": round(last_msg_ago, 1) if last_msg_ago else None,
        "dms_enabled": adapter.enable_dms,
        "admin_nodes": list(adapter.admin_nodes)[:10] if adapter.admin_nodes else [],
        "admin_channels": list(adapter.admin_channels)[:10] if adapter.admin_channels else [],
    }


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status():
    """Full node status — connection, radio, stats, contacts."""
    adapter = _get_adapter()
    return JSONResponse(_adapter_state(adapter))


@router.get("/contacts")
async def get_contacts():
    """List all known contacts with basic info."""
    adapter = _get_adapter()
    if adapter is None:
        return JSONResponse({"error": "Gateway not running"}, status_code=503)

    contacts = adapter._contacts or {}
    type_names = {0: "unknown", 1: "client", 2: "repeater", 3: "room"}
    result = []
    for pubkey, c in contacts.items():
        ctype = c.get("type")
        result.append({
            "name": c.get("adv_name", pubkey[:8]),
            "pubkey_prefix": pubkey[:12],
            "type": ctype,
            "type_name": type_names.get(ctype, "unknown") if ctype is not None else "unknown",
            "lat": c.get("adv_lat"),
            "lon": c.get("adv_lon"),
            "out_path_len": c.get("out_path_len"),
            "out_path": c.get("out_path", ""),
            "last_advert": c.get("last_advert"),
        })

    # Sort: repeaters first, then by name
    result.sort(key=lambda x: (0 if x["type"] == 2 else 1, x["name"].lower()))
    return JSONResponse({"contacts": result, "total": len(result)})


@router.get("/health")
async def get_health():
    """Quick health check — connected, battery, last message."""
    adapter = _get_adapter()
    if adapter is None:
        return JSONResponse({"connected": False, "error": "Gateway not running"})

    conn = adapter._conn
    connected = conn is not None and conn.is_connected
    stats = adapter._stats_cache or {}
    last_msg_ago = time.time() - adapter._last_message_time if adapter._last_message_time else None

    return JSONResponse({
        "connected": connected,
        "battery_mv": stats.get("battery"),
        "uptime_s": stats.get("uptime"),
        "last_message_ago_s": round(last_msg_ago, 1) if last_msg_ago else None,
        "contact_count": len(adapter._contacts or {}),
    })
