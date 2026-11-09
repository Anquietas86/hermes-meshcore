"""Distance helpers for the nearby-nodes tool (stdlib only, no gateway imports)."""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List

EARTH_RADIUS_KM = 6371.0088


def has_location(lat: Any, lon: Any) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    # MeshCore reports 0,0 when no position is set.
    return (lat, lon) != (0.0, 0.0) and -90 <= lat <= 90 and -180 <= lon <= 180


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    deg = (math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                   math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int((deg + 22.5) // 45) % 8]


def nearby(own_lat: Any, own_lon: Any, contacts: Iterable[Dict[str, Any]], *,
           kind: str = "repeater", limit: int = 10) -> Dict[str, Any]:
    """Rank contacts (``contact_summary`` dicts) by distance from our node."""
    if not has_location(own_lat, own_lon):
        return {"success": False, "error": "This node has no location set, so distances are unknown. "
                                            "Set one in the MeshCore app (Settings > Location)."}
    kind = (kind or "repeater").lower()
    rows: List[Dict[str, Any]] = []
    unplaced = 0
    for c in contacts:
        if kind != "any" and c.get("type") != kind:
            continue
        if not has_location(c.get("lat"), c.get("lon")):
            unplaced += 1
            continue
        rows.append({
            "name": c.get("name", ""),
            "pubkey_prefix": c.get("pubkey_prefix", ""),
            "type": c.get("type"),
            "distance_km": round(distance_km(float(own_lat), float(own_lon), float(c["lat"]), float(c["lon"])), 1),
            "direction": bearing(float(own_lat), float(own_lon), float(c["lat"]), float(c["lon"])),
            "hops": c.get("hops"),
            "last_advert": c.get("last_advert"),
        })
    rows.sort(key=lambda r: r["distance_km"])
    limit = max(1, min(int(limit or 10), 50))
    out: Dict[str, Any] = {"success": True, "nodes": rows[:limit], "total_with_location": len(rows)}
    if unplaced:
        out["without_location"] = unplaced
    out["note"] = "hops: 0 = heard directly, -1 = no known route yet (reached by flood)"
    return out
