"""Read-only remote queries to MeshCore repeaters and room servers.

Over the mesh a repeater only answers binary requests (after a login, guest is
fine) and a few anonymous requests (no login, but they need a direct route).
Text CLI commands such as ``ver`` or ``set ...`` are serial-console only, so
they are deliberately not offered here.

Every command below maps to exactly one over-the-air request and returns a
parsed result, not raw hex.
"""

from __future__ import annotations

import asyncio
import os
import struct
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from . import protocol as p

# Firmware suggests a timeout in ms; meshcore_py waits suggested/800 seconds.
_MIN_WAIT_S = 8.0
_MAX_WAIT_S = 60.0


@dataclass(frozen=True)
class RemoteCommand:
    name: str
    kind: str          # "binary" or "anon"
    sub_type: int
    description: str


COMMANDS: Dict[str, RemoteCommand] = {
    "status": RemoteCommand("status", "binary", p.BINREQ_STATUS,
                            "Battery, uptime, queue, noise floor, RSSI/SNR, packet and airtime counters"),
    "telemetry": RemoteCommand("telemetry", "binary", p.BINREQ_TELEMETRY,
                               "Sensor telemetry (Cayenne LPP: voltage, temperature, GPS, ...)"),
    "neighbours": RemoteCommand("neighbours", "binary", p.BINREQ_NEIGHBOURS,
                                "Neighbouring repeaters heard directly, with SNR and last-heard time"),
    "acl": RemoteCommand("acl", "binary", p.BINREQ_ACL,
                         "Access control list (needs an admin login on most repeaters)"),
    "owner": RemoteCommand("owner", "anon", p.ANONREQ_OWNER, "Node name and owner info"),
    "clock": RemoteCommand("clock", "anon", p.ANONREQ_BASIC, "Remote clock"),
    "regions": RemoteCommand("regions", "anon", p.ANONREQ_REGIONS, "Allowed regions"),
}

# Accepted spellings from earlier releases. Only aliases that return the same
# data are kept; "ver", "board", "get name" etc. used to silently return the
# status block and are gone.
ALIASES = {
    "neighbors": "neighbours", "req_neighbours": "neighbours",
    "stats": "status", "stats-core": "status", "stats-radio": "status", "stats-packets": "status",
    "req_status": "status", "req_telemetry": "telemetry", "req_acl": "acl",
    "get owner.info": "owner", "req_owner": "owner", "req_clock": "clock",
    "region list allowed": "regions", "req_regions": "regions",
}

ALL_COMMANDS = ("status", "neighbours", "owner", "clock")


def resolve_command(command: str) -> Optional[str]:
    key = (command or "").strip().lower()
    if key == "all" or key in COMMANDS:
        return key
    return ALIASES.get(key)


def supported_commands_text() -> str:
    return ", ".join(list(COMMANDS) + ["all"])


# ── Response parsers ─────────────────────────────────────────────────────

def parse_status(data: bytes) -> Dict[str, Any]:
    if len(data) < 52:
        raise ValueError(f"status response too short: {len(data)} < 52")
    (bat, txq, noise, rssi, n_recv, n_sent, airtime, uptime, sent_flood, sent_direct,
     recv_flood, recv_direct, full_evts, snr, direct_dups, flood_dups, rx_airtime) = struct.unpack_from(
        "<HHhhIIIIIIIIHhHHI", data)
    out = {
        "battery_mv": bat, "tx_queue_len": txq, "noise_floor_dbm": noise, "last_rssi_dbm": rssi,
        "packets_recv": n_recv, "packets_sent": n_sent, "tx_airtime_s": airtime, "uptime_s": uptime,
        "sent_flood": sent_flood, "sent_direct": sent_direct, "recv_flood": recv_flood,
        "recv_direct": recv_direct, "full_events": full_evts, "last_snr_db": snr / 4,
        "direct_dups": direct_dups, "flood_dups": flood_dups, "rx_airtime_s": rx_airtime,
    }
    if len(data) >= 56:
        out["recv_errors"] = struct.unpack_from("<I", data, 52)[0]
    return out


def parse_neighbours(data: bytes, prefix_len: int = 4) -> Dict[str, Any]:
    if len(data) < 4:
        raise ValueError("neighbours response too short")
    total, count = struct.unpack_from("<hh", data)
    entry = prefix_len + 5
    if count < 0 or len(data) < 4 + count * entry:
        raise ValueError("neighbours response truncated")
    neighbours = []
    for i in range(count):
        at = 4 + i * entry
        neighbours.append({
            "pubkey_prefix": data[at:at + prefix_len].hex(),
            "secs_ago": struct.unpack_from("<i", data, at + prefix_len)[0],
            "snr_db": struct.unpack_from("<b", data, at + prefix_len + 4)[0] / 4,
        })
    return {"total": total, "returned": count, "neighbours": neighbours}


def parse_acl(data: bytes) -> List[Dict[str, Any]]:
    out = []
    for at in range(0, len(data) - 6, 7):
        key = data[at:at + 6].hex()
        if key != "000000000000":
            out.append({"pubkey_prefix": key, "permissions": data[at + 6]})
    return out


# Cayenne LPP: type -> (name, size, decoder)
_LPP: Dict[int, tuple] = {
    0x00: ("digital_in", 1, lambda b: b[0]),
    0x01: ("digital_out", 1, lambda b: b[0]),
    0x02: ("analog_in", 2, lambda b: struct.unpack(">h", b)[0] / 100),
    0x03: ("analog_out", 2, lambda b: struct.unpack(">h", b)[0] / 100),
    0x65: ("illuminance", 2, lambda b: struct.unpack(">H", b)[0]),
    0x66: ("presence", 1, lambda b: b[0]),
    0x67: ("temperature_c", 2, lambda b: struct.unpack(">h", b)[0] / 10),
    0x68: ("humidity_pct", 1, lambda b: b[0] / 2),
    0x73: ("pressure_hpa", 2, lambda b: struct.unpack(">H", b)[0] / 10),
    0x74: ("voltage_v", 2, lambda b: struct.unpack(">H", b)[0] / 100),
    0x75: ("current_a", 2, lambda b: struct.unpack(">H", b)[0] / 1000),
    0x76: ("frequency_hz", 4, lambda b: struct.unpack(">I", b)[0]),
    0x78: ("percentage", 1, lambda b: b[0]),
    0x79: ("altitude_m", 2, lambda b: struct.unpack(">h", b)[0]),
    0x80: ("power_w", 2, lambda b: struct.unpack(">H", b)[0]),
    0x88: ("gps", 9, lambda b: {
        "lat": int.from_bytes(b[0:3], "big", signed=True) / 10000,
        "lon": int.from_bytes(b[3:6], "big", signed=True) / 10000,
        "alt_m": int.from_bytes(b[6:9], "big", signed=True) / 100,
    }),
}


def parse_lpp(data: bytes) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    at = 0
    while at + 2 <= len(data) and data[at] != 0:
        channel, kind = data[at], data[at + 1]
        spec = _LPP.get(kind)
        if spec is None:
            out.append({"channel": channel, "type": f"0x{kind:02x}", "raw": data[at + 2:].hex()})
            break
        name, size, decode = spec
        chunk = data[at + 2:at + 2 + size]
        if len(chunk) < size:
            break
        out.append({"channel": channel, "type": name, "value": decode(chunk)})
        at += 2 + size
    return out


def _strip_tag(data: bytes) -> bytes:
    # Anonymous replies repeat the 4-byte tag at the start of their data.
    return data[4:] if len(data) >= 4 else b""


def parse_owner(data: bytes) -> Dict[str, Any]:
    text = _strip_tag(data).decode("utf-8", "replace").strip("\x00")
    name, _, owner = text.partition("\n")
    return {"name": name.strip(), "owner": owner.strip("\x00").strip()}


def parse_clock(data: bytes) -> Dict[str, Any]:
    body = _strip_tag(data)
    if len(body) < 4:
        return {"raw": data.hex()}
    return {"remote_time": int.from_bytes(body[:4], "little")}


def parse_regions(data: bytes) -> Dict[str, Any]:
    return {"regions": _strip_tag(data).decode("utf-8", "replace").strip("\x00").strip()}


_PARSERS: Dict[str, Callable[[bytes], Any]] = {
    "status": parse_status,
    "telemetry": parse_lpp,
    "neighbours": parse_neighbours,
    "acl": parse_acl,
    "owner": parse_owner,
    "clock": parse_clock,
    "regions": parse_regions,
}


# ── Query runner ─────────────────────────────────────────────────────────

class RemoteQueryError(Exception):
    pass


def _wait_seconds(sent: Dict[str, Any]) -> float:
    suggested = sent.get("suggested_timeout_ms") or 4000
    return max(_MIN_WAIT_S, min(_MAX_WAIT_S, suggested / 800))


class RepeaterSession:
    """Runs remote queries against one contact on an open :class:`MeshCoreClient`.

    The owning adapter routes pushes into :meth:`on_push`; responses are
    matched by tag (binary/anon replies) or pubkey prefix (login).
    """

    def __init__(self, client: "p.MeshCoreClient") -> None:
        self._client = client
        self._tag_waiters: Dict[str, asyncio.Future] = {}
        self._login_waiters: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()   # one mesh request at a time: airtime is precious

    def on_push(self, frame: "p.Frame") -> bool:
        """Resolve a waiter from a push frame. Returns True when the frame was consumed."""
        if frame.code == p.PUSH_BINARY_RESPONSE:
            try:
                tag, data = p.parse_binary_response(frame.body)
            except ValueError:
                return False
            fut = self._tag_waiters.pop(tag, None)
            if fut is not None and not fut.done():
                fut.set_result(data)
                return True
            return False
        if frame.code in (p.PUSH_LOGIN_SUCCESS, p.PUSH_LOGIN_FAILED):
            info = p.parse_login_push(frame.body)
            prefix = info.get("pubkey_prefix", "")
            fut = self._login_waiters.pop(prefix, None)
            if fut is None and len(self._login_waiters) == 1 and not prefix:
                fut = self._login_waiters.pop(next(iter(self._login_waiters)))
            if fut is not None and not fut.done():
                info["ok"] = frame.code == p.PUSH_LOGIN_SUCCESS
                fut.set_result(info)
                return True
        return False

    async def _login(self, contact: Dict[str, Any], password: str) -> Dict[str, Any]:
        pubkey = bytes.fromhex(contact["public_key"])
        prefix = contact["public_key"][:12]
        fut = asyncio.get_running_loop().create_future()
        self._login_waiters[prefix] = fut
        try:
            reply = await self._client.request(p.cmd_login(pubkey, password), [p.RESP_MSG_SENT])
            if reply.is_error:
                raise RemoteQueryError(f"node refused login request: {reply.error_text()}")
            sent = p.parse_msg_sent(reply.body)
            try:
                info = await asyncio.wait_for(fut, timeout=_wait_seconds(sent))
            except asyncio.TimeoutError:
                raise RemoteQueryError("login timed out: no reply from the repeater") from None
            if not info.get("ok"):
                raise RemoteQueryError("login rejected: wrong password or guest access disabled")
            return info
        finally:
            self._login_waiters.pop(prefix, None)

    async def _request(self, contact: Dict[str, Any], cmd: RemoteCommand) -> bytes:
        pubkey = bytes.fromhex(contact["public_key"])
        if cmd.kind == "binary":
            extra = b""
            if cmd.sub_type == p.BINREQ_NEIGHBOURS:
                # version 0, up to 255 entries, offset 0, order 0, 4-byte prefixes, random nonce
                extra = b"\x00\xff\x00\x00\x00\x04" + os.urandom(4)
            elif cmd.sub_type == p.BINREQ_ACL:
                extra = b"\x00\x00"
            payload = p.cmd_binary_req(pubkey, cmd.sub_type, extra)
        else:
            hops = contact.get("out_path_len", -1)
            if hops is None or hops < 0:
                raise RemoteQueryError(
                    f"'{cmd.name}' is an anonymous request and repeaters only answer those over a "
                    "direct route; this contact has no known path yet (try again after it has been reached directly)")
            payload = p.cmd_anon_req(pubkey, cmd.sub_type, p.encode_reply_path(
                hops, contact.get("out_path", ""), contact.get("out_path_hash_mode", 0)))
        reply = await self._client.request(payload, [p.RESP_MSG_SENT])
        if reply.is_error:
            raise RemoteQueryError(f"node refused the request: {reply.error_text()}")
        sent = p.parse_msg_sent(reply.body)
        fut = asyncio.get_running_loop().create_future()
        self._tag_waiters[sent["expected_ack"]] = fut
        try:
            return await asyncio.wait_for(fut, timeout=_wait_seconds(sent))
        except asyncio.TimeoutError:
            raise RemoteQueryError(f"'{cmd.name}' timed out: no reply from the repeater") from None
        finally:
            self._tag_waiters.pop(sent["expected_ack"], None)

    async def run(self, contact: Dict[str, Any], command: str, password: str = "") -> Dict[str, Any]:
        resolved = resolve_command(command)
        if resolved is None:
            return {"success": False, "error": f"Unknown command {command!r}. Supported: {supported_commands_text()}"}
        names = list(ALL_COMMANDS) if resolved == "all" else [resolved]
        results: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        async with self._lock:
            logged_in = False
            for name in names:
                cmd = COMMANDS[name]
                try:
                    if cmd.kind == "binary" and not logged_in:
                        await self._login(contact, password)
                        logged_in = True
                    data = await self._request(contact, cmd)
                    results[name] = _PARSERS[name](data)
                except (RemoteQueryError, p.MeshCoreError, ValueError) as exc:
                    errors[name] = str(exc)
                    if isinstance(exc, p.MeshCoreClosed):
                        break
        out: Dict[str, Any] = {
            "success": bool(results),
            "node": {
                "name": contact.get("adv_name", ""),
                "pubkey_prefix": contact.get("public_key", "")[:12],
                "type": contact.get("type_name", ""),
                "hops": contact.get("out_path_len"),
            },
            "results": results,
        }
        if errors:
            out["errors"] = errors
            if not results:
                out["error"] = "; ".join(f"{k}: {v}" for k, v in errors.items())
        return out
