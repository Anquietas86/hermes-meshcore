"""MeshCore companion-radio protocol over TCP (stdlib only).

Wire format (companion protocol):

* host -> node frame: ``0x3C`` + uint16 LE length + payload
* node -> host frame: ``0x3E`` + uint16 LE length + payload

The first payload byte is a command code (host -> node) or a response/push
code (node -> host). Codes below ``0x80`` are replies to the command that is
currently in flight; codes at or above ``0x80`` are unsolicited pushes.

:class:`MeshCoreClient` owns a single reader task that splits incoming frames
into those two streams. Commands are serialised by a lock and read their reply
from the response queue, so a push can never be mistaken for a reply and a
reply can never be swallowed by a push handler.

Parsers take the payload *without* its leading code byte.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import socket
import struct
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Framing ──────────────────────────────────────────────────────────────

FRAME_TO_NODE = 0x3C
FRAME_FROM_NODE = 0x3E
MAX_FRAME_SIZE = 300  # sanity bound; real companion frames are well under this

# ── Command codes (host -> node) ─────────────────────────────────────────

CMD_APP_START = 0x01
CMD_SEND_TXT_MSG = 0x02
CMD_SEND_CHANNEL_TXT_MSG = 0x03
CMD_GET_CONTACTS = 0x04
CMD_SEND_SELF_ADVERT = 0x07
CMD_SYNC_NEXT_MESSAGE = 0x0A
CMD_GET_BATT_AND_STORAGE = 0x14
CMD_DEVICE_QUERY = 0x16
CMD_SEND_LOGIN = 0x1A
CMD_GET_CHANNEL = 0x1F
CMD_BINARY_REQ = 0x32
CMD_GET_STATS = 0x38
CMD_RESET_PATH = 0x0D
CMD_SEND_ANON_REQ = 0x39

# Sub-types for CMD_BINARY_REQ (require a login, guest is fine)
BINREQ_STATUS = 0x01
BINREQ_TELEMETRY = 0x03
BINREQ_ACL = 0x05
BINREQ_NEIGHBOURS = 0x06

# Sub-types for CMD_SEND_ANON_REQ (no login, need a direct route)
ANONREQ_REGIONS = 0x01
ANONREQ_OWNER = 0x02
ANONREQ_BASIC = 0x03

# ── Reply codes (node -> host, < 0x80) ───────────────────────────────────

RESP_OK = 0x00
RESP_ERROR = 0x01
RESP_CONTACT_START = 0x02
RESP_CONTACT = 0x03
RESP_CONTACT_END = 0x04
RESP_SELF_INFO = 0x05
RESP_MSG_SENT = 0x06
RESP_CONTACT_MSG = 0x07
RESP_CHANNEL_MSG = 0x08
RESP_NO_MORE_MSGS = 0x0A
RESP_BATTERY = 0x0C
RESP_DEVICE_INFO = 0x0D
RESP_CONTACT_MSG_V3 = 0x10
RESP_CHANNEL_MSG_V3 = 0x11
RESP_CHANNEL_INFO = 0x12
RESP_STATS = 0x18

MESSAGE_REPLIES = frozenset({RESP_CONTACT_MSG, RESP_CONTACT_MSG_V3, RESP_CHANNEL_MSG, RESP_CHANNEL_MSG_V3})

# ── Push codes (node -> host, >= 0x80) ───────────────────────────────────

PUSH_ADVERT = 0x80
PUSH_PATH_UPDATE = 0x81
PUSH_ACK = 0x82
PUSH_MESSAGES_WAITING = 0x83
PUSH_LOGIN_SUCCESS = 0x85
PUSH_LOGIN_FAILED = 0x86
PUSH_STATUS_RESPONSE = 0x87
PUSH_NEW_ADVERT = 0x8A
PUSH_TELEMETRY_RESPONSE = 0x8B
PUSH_BINARY_RESPONSE = 0x8C

ERROR_CODES = {
    1: "unsupported_command", 2: "not_found", 3: "table_full", 4: "bad_state",
    5: "file_io_error", 6: "illegal_arg",
}

# Text types carried in contact messages
TXT_PLAIN = 0
TXT_CLI_DATA = 1
TXT_SIGNED_PLAIN = 2

CONTACT_TYPES = {0: "unknown", 1: "client", 2: "repeater", 3: "room", 4: "sensor"}


class MeshCoreError(Exception):
    """Protocol-level failure (bad frame, error reply)."""


class MeshCoreTimeout(MeshCoreError):
    """No reply within the deadline."""


class MeshCoreClosed(MeshCoreError):
    """The TCP connection is gone."""


@dataclass(frozen=True)
class Frame:
    code: int
    body: bytes

    @property
    def is_error(self) -> bool:
        return self.code == RESP_ERROR

    def error_text(self) -> str:
        if not self.is_error:
            return ""
        code = self.body[0] if self.body else 0
        return ERROR_CODES.get(code, f"error_{code}")


def encode_frame(payload: bytes) -> bytes:
    if not payload:
        raise ValueError("frame payload must include a command code")
    if len(payload) > MAX_FRAME_SIZE:
        raise ValueError(f"frame payload too large: {len(payload)} > {MAX_FRAME_SIZE}")
    return bytes([FRAME_TO_NODE]) + len(payload).to_bytes(2, "little") + payload


class FrameDecoder:
    """Incremental decoder for node -> host frames.

    Junk before a marker and frames with an impossible length are skipped
    (logged), never fatal: a stray byte from firmware debug output must not
    tear down the link.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[Frame]:
        self._buf.extend(data)
        frames: List[Frame] = []
        while True:
            start = self._buf.find(bytes([FRAME_FROM_NODE]))
            if start < 0:
                if self._buf:
                    logger.debug("MeshCore: dropping %d junk bytes", len(self._buf))
                self._buf.clear()
                return frames
            if start:
                logger.debug("MeshCore: skipping %d junk bytes", start)
                del self._buf[:start]
            if len(self._buf) < 3:
                return frames
            size = int.from_bytes(self._buf[1:3], "little")
            if not 1 <= size <= MAX_FRAME_SIZE:
                logger.debug("MeshCore: impossible frame size %d, resyncing", size)
                del self._buf[:1]
                continue
            if len(self._buf) < 3 + size:
                return frames
            payload = bytes(self._buf[3:3 + size])
            del self._buf[:3 + size]
            frames.append(Frame(payload[0], payload[1:]))


# ── Parsers ──────────────────────────────────────────────────────────────

def _require(body: bytes, minimum: int, what: str) -> None:
    if len(body) < minimum:
        raise ValueError(f"{what} payload too short: {len(body)} < {minimum}")


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")


def _path_byte(value: int) -> Tuple[int, int]:
    """Decode a path-length byte into ``(hops, hash_size)``; 0xFF is flood/direct (-1, 0)."""
    if value == 0xFF:
        return -1, 0
    return value & 0x3F, (value >> 6) + 1


def parse_self_info(body: bytes) -> Dict[str, Any]:
    _require(body, 57, "SELF_INFO")
    tm = body[45]
    return {
        "adv_type": body[0],
        "tx_power": body[1],
        "max_tx_power": body[2],
        "public_key": body[3:35].hex(),
        "adv_lat": struct.unpack_from("<i", body, 35)[0] / 1e6,
        "adv_lon": struct.unpack_from("<i", body, 39)[0] / 1e6,
        "multi_acks": body[43],
        "adv_loc_policy": body[44],
        "telemetry_mode_env": (tm >> 4) & 0b11,
        "telemetry_mode_loc": (tm >> 2) & 0b11,
        "telemetry_mode_base": tm & 0b11,
        "manual_add_contacts": body[46] > 0,
        "radio_freq": struct.unpack_from("<I", body, 47)[0] / 1000,
        "radio_bw": struct.unpack_from("<I", body, 51)[0] / 1000,
        "radio_sf": body[55],
        "radio_cr": body[56],
        "name": _cstr(body[57:]),
    }


def parse_device_info(body: bytes) -> Dict[str, Any]:
    _require(body, 1, "DEVICE_INFO")
    info: Dict[str, Any] = {"fw_ver": body[0]}
    if body[0] >= 3:
        _require(body, 79, "DEVICE_INFO v3")
        info.update(
            max_contacts=body[1] * 2,
            max_channels=body[2],
            fw_build=_cstr(body[7:19]),
            model=_cstr(body[19:59]),
            ver=_cstr(body[59:79]),
        )
        if len(body) > 79:
            info["repeat"] = body[79] != 0
        if len(body) > 80:
            info["path_hash_mode"] = body[80]
    return info


def parse_battery(body: bytes) -> Dict[str, Any]:
    _require(body, 2, "BATTERY")
    result: Dict[str, Any] = {"battery_mv": int.from_bytes(body[:2], "little")}
    if len(body) >= 10:
        result["used_kb"] = int.from_bytes(body[2:6], "little")
        result["total_kb"] = int.from_bytes(body[6:10], "little")
    return result


def parse_msg_sent(body: bytes) -> Dict[str, Any]:
    _require(body, 9, "MSG_SENT")
    return {
        "flood": body[0] == 1,
        "expected_ack": body[1:5].hex(),
        "suggested_timeout_ms": int.from_bytes(body[5:9], "little"),
    }


def parse_contact_msg(body: bytes, *, v3: bool) -> Dict[str, Any]:
    offset = 3 if v3 else 0
    _require(body, offset + 12, "CONTACT_MSG")
    msg: Dict[str, Any] = {"type": "dm"}
    if v3:
        msg["snr"] = struct.unpack_from("<b", body, 0)[0] / 4
    msg["pubkey_prefix"] = body[offset:offset + 6].hex()
    msg["hops"], _ = _path_byte(body[offset + 6])
    msg["txt_type"] = body[offset + 7]
    msg["sender_timestamp"] = int.from_bytes(body[offset + 8:offset + 12], "little")
    text_at = offset + 12
    if msg["txt_type"] == TXT_SIGNED_PLAIN:
        _require(body, text_at + 4, "signed CONTACT_MSG")
        msg["signature"] = body[text_at:text_at + 4].hex()
        text_at += 4
    msg["text"] = body[text_at:].rstrip(b"\x00").decode("utf-8", "replace")
    return msg


def parse_channel_msg(body: bytes, *, v3: bool) -> Dict[str, Any]:
    offset = 3 if v3 else 0
    _require(body, offset + 7, "CHANNEL_MSG")
    msg: Dict[str, Any] = {"type": "channel"}
    if v3:
        msg["snr"] = struct.unpack_from("<b", body, 0)[0] / 4
    msg["channel_idx"] = body[offset]
    msg["hops"], _ = _path_byte(body[offset + 1])
    msg["txt_type"] = body[offset + 2]
    msg["sender_timestamp"] = int.from_bytes(body[offset + 3:offset + 7], "little")
    msg["text"] = body[offset + 7:].rstrip(b"\x00").decode("utf-8", "replace")
    return msg


def parse_message(frame: Frame) -> Optional[Dict[str, Any]]:
    if frame.code == RESP_CONTACT_MSG:
        return parse_contact_msg(frame.body, v3=False)
    if frame.code == RESP_CONTACT_MSG_V3:
        return parse_contact_msg(frame.body, v3=True)
    if frame.code == RESP_CHANNEL_MSG:
        return parse_channel_msg(frame.body, v3=False)
    if frame.code == RESP_CHANNEL_MSG_V3:
        return parse_channel_msg(frame.body, v3=True)
    return None


def parse_contact(body: bytes) -> Dict[str, Any]:
    _require(body, 147, "CONTACT")
    plen = body[34]
    if plen == 0xFF:
        hops, hash_mode = -1, -1
    else:
        hops, hash_mode = plen & 0x3F, plen >> 6
    path_raw = body[35:99]
    used = hops * (hash_mode + 1) if hops > 0 else 0
    ctype = body[32]
    return {
        "public_key": body[0:32].hex(),
        "type": ctype,
        "type_name": CONTACT_TYPES.get(ctype, "unknown"),
        "flags": body[33],
        "out_path_len": hops,
        "out_path_hash_mode": hash_mode,
        "out_path": path_raw[:used].hex(),
        "adv_name": _cstr(body[99:131]),
        "last_advert": int.from_bytes(body[131:135], "little"),
        "adv_lat": struct.unpack_from("<i", body, 135)[0] / 1e6,
        "adv_lon": struct.unpack_from("<i", body, 139)[0] / 1e6,
        "lastmod": int.from_bytes(body[143:147], "little"),
    }


def parse_channel_info(body: bytes) -> Dict[str, Any]:
    _require(body, 33, "CHANNEL_INFO")
    return {"channel_idx": body[0], "channel_name": _cstr(body[1:33])}


def parse_stats(body: bytes) -> Dict[str, Any]:
    _require(body, 1, "STATS")
    kind, data = body[0], body[1:]
    if kind == 0:
        _require(data, 9, "core STATS")
        bat, uptime, errors, queue = struct.unpack_from("<HIHB", data)
        return {"kind": "core", "battery_mv": bat, "uptime_s": uptime, "errors": errors, "queue_len": queue}
    if kind == 1:
        _require(data, 12, "radio STATS")
        noise, rssi, snr, tx_air, rx_air = struct.unpack_from("<hbbII", data)
        return {"kind": "radio", "noise_floor_dbm": noise, "last_rssi_dbm": rssi,
                "last_snr_db": snr / 4, "tx_air_s": tx_air, "rx_air_s": rx_air}
    if kind == 2:
        _require(data, 24, "packet STATS")
        recv, sent, flood_tx, direct_tx, flood_rx, direct_rx = struct.unpack_from("<6I", data)
        out = {"kind": "packets", "recv": recv, "sent": sent, "flood_tx": flood_tx,
               "direct_tx": direct_tx, "flood_rx": flood_rx, "direct_rx": direct_rx}
        if len(data) >= 28:
            out["recv_errors"] = struct.unpack_from("<I", data, 24)[0]
        return out
    return {"kind": f"unknown_{kind}"}


def parse_login_push(body: bytes) -> Dict[str, Any]:
    """LOGIN_SUCCESS / LOGIN_FAILED push: permissions(1) + pubkey_prefix(6) + optional extras."""
    out: Dict[str, Any] = {}
    if body:
        out["permissions"] = body[0]
        out["is_admin"] = bool(body[0] & 1)
    if len(body) >= 7:
        out["pubkey_prefix"] = body[1:7].hex()
    return out


def parse_binary_response(body: bytes) -> Tuple[str, bytes]:
    """BINARY_RESPONSE push: reserved(1) + tag(4) + data. Returns ``(tag_hex, data)``."""
    _require(body, 5, "BINARY_RESPONSE")
    return body[1:5].hex(), body[5:]


# ── Command builders ─────────────────────────────────────────────────────

def cmd_app_start(app_name: str = "hermes") -> bytes:
    return bytes([CMD_APP_START, 0x03]) + b"      " + app_name.encode("ascii", "replace")[:24]


def cmd_send_dm(dest_pubkey: bytes, text: bytes, timestamp: int, attempt: int = 0) -> bytes:
    if len(dest_pubkey) < 6:
        raise ValueError("destination needs at least a 6-byte pubkey prefix")
    return bytes([CMD_SEND_TXT_MSG, TXT_PLAIN, attempt & 0xFF]) + timestamp.to_bytes(4, "little") + dest_pubkey[:6] + text


def cmd_send_channel(channel_idx: int, text: bytes, timestamp: int) -> bytes:
    if not 0 <= channel_idx <= 255:
        raise ValueError(f"channel index out of range: {channel_idx}")
    return bytes([CMD_SEND_CHANNEL_TXT_MSG, TXT_PLAIN, channel_idx]) + timestamp.to_bytes(4, "little") + text


def cmd_login(pubkey: bytes, password: str) -> bytes:
    if len(pubkey) != 32:
        raise ValueError("login needs the full 32-byte public key")
    return bytes([CMD_SEND_LOGIN]) + pubkey + password.encode("utf-8")


def cmd_binary_req(pubkey: bytes, sub_type: int, data: bytes = b"") -> bytes:
    if len(pubkey) != 32:
        raise ValueError("binary requests need the full 32-byte public key")
    return bytes([CMD_BINARY_REQ]) + pubkey + bytes([sub_type]) + data


def encode_reply_path(hops: int, path_hex: str, hash_mode: int) -> bytes:
    """Reply path for an anonymous request: the contact's out-path reversed by hop.

    Mirrors meshcore_py: ``hops | (hash_mode << 6)`` then the hop hashes in
    reverse order. A flood contact (hops < 0) gets a zero-hop reply path.
    """
    if hops is None or hops <= 0 or hash_mode is None or hash_mode < 0 or hash_mode > 2:
        return b"\x00"
    size = hash_mode + 1
    raw = bytes.fromhex(path_hex or "")
    hops = min(hops, len(raw) // size, 63, 64 // size)
    path = b"".join(raw[i * size:(i + 1) * size] for i in range(hops - 1, -1, -1))
    return bytes([hops | (hash_mode << 6)]) + path


def cmd_anon_req(pubkey: bytes, sub_type: int, reply_path: bytes) -> bytes:
    if len(pubkey) != 32:
        raise ValueError("anonymous requests need the full 32-byte public key")
    return bytes([CMD_SEND_ANON_REQ]) + pubkey + bytes([sub_type]) + reply_path


# ── Client ───────────────────────────────────────────────────────────────

PushHandler = Callable[[Frame], Awaitable[None]]
ClosedHandler = Callable[[Optional[BaseException]], Awaitable[None]]


def cmd_reset_path(pubkey: bytes) -> bytes:
    if len(pubkey) != 32:
        raise ValueError("reset path needs the full 32-byte pubkey")
    return bytes([CMD_RESET_PATH]) + pubkey


def parse_ack(body: bytes) -> Dict[str, Any]:
    """ACK push: code(4) [+ round-trip ms(4) on firmware since early 2025]."""
    _require(body, 4, "ACK")
    out: Dict[str, Any] = {"code": body[:4].hex()}
    if len(body) >= 8:
        out["trip_ms"] = int.from_bytes(body[4:8], "little")
    return out


# MeshCore packet overhead on top of the text: header, path, MAC, timestamp, flags.
PACKET_OVERHEAD_BYTES = 24


def lora_airtime_s(payload_bytes: int, sf: int, bw_khz: float, cr: int, preamble: int = 16) -> float:
    """Time on air for one LoRa packet (Semtech AN1200.13), explicit header, CRC on.

    ``cr`` is the denominator of the coding rate (5..8 for 4/5..4/8).
    Falls back to conservative SF11/250kHz numbers when the radio settings are unknown.
    """
    if not (6 <= int(sf or 0) <= 12) or not bw_khz or bw_khz <= 0:
        sf, bw_khz = 11, 250.0
    cr_idx = min(4, max(1, (int(cr or 5) - 4) if int(cr or 5) >= 5 else int(cr or 1)))
    t_sym = (2 ** sf) / (bw_khz * 1000.0)
    low_dr = 1 if t_sym > 0.016 else 0
    n = 8 * payload_bytes - 4 * sf + 28 + 16
    payload_symbols = 8 + max(math.ceil(n / (4 * (sf - 2 * low_dr))) * (cr_idx + 4), 0)
    return (preamble + 4.25 + payload_symbols) * t_sym


class MeshCoreClient:
    """One TCP session with a companion node.

    ``on_push`` receives every push frame (>= 0x80) on a dedicated dispatcher
    task. ``on_closed`` is awaited once when the link drops unexpectedly
    (never after :meth:`close`).
    """

    def __init__(self, host: str, port: int, *, on_push: Optional[PushHandler] = None,
                 on_closed: Optional[ClosedHandler] = None, debug: bool = False) -> None:
        self.host = host
        self.port = port
        self._on_push = on_push
        self._on_closed = on_closed
        self._debug = debug
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._responses: "asyncio.Queue[Frame]" = asyncio.Queue()
        self._pushes: "asyncio.Queue[Frame]" = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._tasks: List[asyncio.Task] = []
        self._closing = False
        self._closed_event = asyncio.Event()
        self._read_transport = None  # serial only: the read side is a separate pipe transport

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._closed_event.is_set()

    async def open(self, timeout: float = 15.0) -> None:
        if self.host.startswith("serial:"):
            self._reader, self._writer, self._read_transport = await open_serial(self.host[7:])
            self._start_tasks()
            return
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=timeout)
        sock = self._writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError, AttributeError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                for opt, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
                    if hasattr(socket, opt):
                        sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), value)
        self._start_tasks()

    def _start_tasks(self) -> None:
        self._tasks = [
            asyncio.create_task(self._read_loop(), name="meshcore-reader"),
            asyncio.create_task(self._push_loop(), name="meshcore-push"),
        ]

    async def close(self) -> None:
        self._closing = True
        self._closed_event.set()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()
        for task in self._tasks:
            if task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._tasks = []
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer.wait_closed(), timeout=2.0)
        if self._read_transport is not None:
            self._read_transport.close()
            self._read_transport = None
        self._reader = self._writer = None

    async def _read_loop(self) -> None:
        decoder = FrameDecoder()
        error: Optional[BaseException] = None
        try:
            while True:
                data = await self._reader.read(1024)
                if not data:
                    error = MeshCoreClosed("node closed the TCP connection")
                    break
                for frame in decoder.feed(data):
                    if self._debug:
                        logger.info("MeshCore rx 0x%02x %s", frame.code, frame.body[:48].hex())
                    (self._pushes if frame.code >= 0x80 else self._responses).put_nowait(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # network error
            error = exc
        self._closed_event.set()
        if not self._closing and self._on_closed is not None:
            try:
                await self._on_closed(error)
            except Exception:
                logger.exception("MeshCore: on_closed handler failed")

    async def _push_loop(self) -> None:
        while True:
            frame = await self._pushes.get()
            if self._on_push is None:
                continue
            try:
                await self._on_push(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MeshCore: push handler failed for 0x%02x", frame.code)

    async def _send(self, payload: bytes) -> None:
        if not self.connected:
            raise MeshCoreClosed("not connected")
        if self._debug:
            logger.info("MeshCore tx 0x%02x %s", payload[0], payload[1:48].hex())
        self._writer.write(encode_frame(payload))
        await self._writer.drain()

    async def _next_response(self, deadline: float) -> Frame:
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise MeshCoreTimeout("no reply from node")
        get = asyncio.ensure_future(self._responses.get())
        closed = asyncio.ensure_future(self._closed_event.wait())
        try:
            done, _ = await asyncio.wait({get, closed}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        finally:
            closed.cancel()
            if not get.done():
                get.cancel()
        if get in done:
            return get.result()
        if closed in done:
            raise MeshCoreClosed("connection lost while waiting for a reply")
        raise MeshCoreTimeout("no reply from node")

    def _drain_stale(self) -> None:
        while not self._responses.empty():
            frame = self._responses.get_nowait()
            logger.debug("MeshCore: discarding stale reply 0x%02x", frame.code)

    async def request(self, payload: bytes, accept: Iterable[int], timeout: float = 10.0) -> Frame:
        """Send one command and return its reply (one of ``accept`` or an ERROR frame)."""
        wanted = set(accept) | {RESP_ERROR}
        deadline = asyncio.get_running_loop().time() + timeout
        async with self._locked(deadline):
            self._drain_stale()
            await self._send(payload)
            while True:
                frame = await self._next_response(deadline)
                if frame.code in wanted:
                    return frame
                logger.debug("MeshCore: ignoring unexpected reply 0x%02x to 0x%02x", frame.code, payload[0])

    async def _abandon_acquire(self, acquire: "asyncio.Future") -> None:
        acquire.cancel()
        await asyncio.wait({acquire})
        if not acquire.cancelled():  # it won the race: give the lock back
            self._lock.release()

    @contextlib.asynccontextmanager
    async def _locked(self, deadline: float):
        """Hold the request lock; time spent queueing counts against the caller's timeout."""
        remaining = deadline - asyncio.get_running_loop().time()
        # Not asyncio.wait_for: on Python 3.11 a timeout racing the acquire can leak the lock.
        acquire = asyncio.ensure_future(self._lock.acquire())
        try:
            done, _ = await asyncio.wait({acquire}, timeout=max(remaining, 0.001))
        except BaseException:
            await self._abandon_acquire(acquire)
            raise
        if not done:
            await self._abandon_acquire(acquire)
            raise MeshCoreTimeout("node busy: request queue did not clear in time")
        try:
            yield
        finally:
            self._lock.release()

    async def request_contacts(self, timeout: float = 20.0) -> List[Dict[str, Any]]:
        deadline = asyncio.get_running_loop().time() + timeout
        async with self._locked(deadline):
            self._drain_stale()
            await self._send(bytes([CMD_GET_CONTACTS]))
            first = await self._next_response(deadline)
            if first.code != RESP_CONTACT_START:
                raise MeshCoreError(f"GET_CONTACTS failed: {first.error_text() or hex(first.code)}")
            contacts: List[Dict[str, Any]] = []
            while True:
                frame = await self._next_response(deadline)
                if frame.code == RESP_CONTACT_END:
                    return contacts
                if frame.code == RESP_CONTACT:
                    try:
                        contacts.append(parse_contact(frame.body))
                    except ValueError as exc:
                        logger.debug("MeshCore: skipping malformed contact: %s", exc)

    async def wait_closed(self) -> None:
        await self._closed_event.wait()


# ── USB serial (experimental) ────────────────────────────────────────────

SERIAL_BAUD = 115200


async def open_serial(path: str, baud: int = SERIAL_BAUD):
    """Open a USB-serial companion as an asyncio stream pair (POSIX, stdlib only).

    Selected with ``MESHCORE_HOST=serial:/dev/ttyUSB0``. The companion uses the
    same framing over serial as over TCP. Experimental: not tested on hardware.
    """
    import termios
    import tty

    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        speed = getattr(termios, f"B{baud}")
        attrs[4] = attrs[5] = speed
        attrs[2] |= termios.CLOCAL | termios.CREAD
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        read_file = os.fdopen(fd, "rb", buffering=0)
        write_file = os.fdopen(os.dup(fd), "wb", buffering=0)
    except Exception:
        os.close(fd)
        raise
    read_transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), read_file)
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, write_file)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer, read_transport
