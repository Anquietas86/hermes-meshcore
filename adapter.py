"""
MeshCore Platform Adapter for Hermes Agent — RAW BINARY PROTOCOL.

Talks the MeshCore companion binary protocol directly over TCP.
No meshcore_py dependency. Handles TCP framing, command dispatch,
response parsing, and message routing internally.

Protocol reference:
  Send frame: 0x3C + 2-byte LE size + payload
  Recv frame: 0x3E + 2-byte LE size + payload
  Commands: single-byte opcode + args (see packets.py CommandType)
  Responses: single-byte type + payload (see packets.py PacketType)

Configuration via environment variables::

    MESHCORE_HOST=192.168.0.141
    MESHCORE_PORT=5000
    MESHCORE_BOT_NAME=meshcore-bot
    MESHCORE_ADMIN_NODES=your-pubkey-prefix
    MESHCORE_MONITOR_CHANNELS=1
    MESHCORE_ENABLE_DMS=true
"""

import asyncio
import io
import json
import logging
import os
import struct
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform


# ── Protocol constants ────────────────────────────────────────────────────

# TCP framing
FRAME_SEND_MARKER = 0x3C
FRAME_RECV_MARKER = 0x3E
MAX_FRAME_SIZE = 300

# Command opcodes
CMD_APP_START = 0x01
CMD_SEND_TXT_MSG = 0x02
CMD_SEND_CHANNEL_TXT_MSG = 0x03
CMD_GET_CONTACTS = 0x04
CMD_SYNC_NEXT_MESSAGE = 0x0A
CMD_SEND_SELF_ADVERT = 0x07
CMD_DEVICE_QUERY = 0x16
CMD_GET_BATT = 0x14
CMD_GET_CHANNEL = 0x1F
CMD_SET_CHANNEL = 0x20
CMD_SET_RADIO = 0x0B
CMD_SET_TX_POWER = 0x0C
CMD_SET_NAME = 0x08
CMD_REBOOT = 0x13
CMD_GET_STATS = 0x38
CMD_RESET_PATH = 0x0D
CMD_GET_SELF_TELEMETRY = 0x27
CMD_SET_OTHER_PARAMS = 0x26
CMD_SEND_LOGIN = 0x1a  # Login to remote repeater/room server
CMD_BINARY_REQ = 0x32   # Send binary request to remote node
CMD_SEND_ANON_REQ = 0x39  # Send anonymous request to remote node

# Binary request types (sub-opcodes for CMD_BINARY_REQ)
BINREQ_STATUS = 0x01      # Request status from repeater
BINREQ_KEEP_ALIVE = 0x02  # Keep-alive ping
BINREQ_TELEMETRY = 0x03   # Request telemetry data
BINREQ_MMA = 0x04         # Message metadata archive
BINREQ_ACL = 0x05         # Request access control list
BINREQ_NEIGHBOURS = 0x06  # Request neighbour list

# Anonymous request types (sub-opcodes for CMD_SEND_ANON_REQ)
ANONREQ_REGIONS = 0x01    # Request regions list
ANONREQ_OWNER = 0x02      # Request owner information
ANONREQ_BASIC = 0x03      # Request basic info (remote clock)

# Response packet types
PKT_OK = 0x00
PKT_ERROR = 0x01
PKT_CONTACT_START = 0x02
PKT_CONTACT = 0x03
PKT_CONTACT_END = 0x04
PKT_SELF_INFO = 0x05
PKT_MSG_SENT = 0x06
PKT_CONTACT_MSG_RECV = 0x07
PKT_CHANNEL_MSG_RECV = 0x08
PKT_CURRENT_TIME = 0x09
PKT_NO_MORE_MSGS = 0x0A
PKT_BATTERY = 0x0C
PKT_DEVICE_INFO = 0x0D
PKT_CONTACT_MSG_RECV_V3 = 0x10
PKT_CHANNEL_MSG_RECV_V3 = 0x11
PKT_CHANNEL_INFO = 0x12
PKT_STATS = 0x18
PKT_TELEMETRY_RESPONSE = 0x8B
PKT_LOGIN_SUCCESS = 0x85  # Remote login accepted
PKT_LOGIN_FAILED = 0x86   # Remote login rejected
PKT_STATUS_RESPONSE = 0x87  # Status response from remote node
PKT_BINARY_RESPONSE = 0x8C  # Binary response from remote node

# Stats sub-types
STATS_TYPE_CORE = 0x00
STATS_TYPE_RADIO = 0x01
STATS_TYPE_PACKETS = 0x02

# Push notifications
PKT_ACK = 0x82
PKT_MESSAGES_WAITING = 0x83
PKT_ADVERTISEMENT = 0x80
PKT_NEW_ADVERT = 0x8A

# Error codes
ERROR_CODES = {
    1: "generic_error", 2: "invalid_command", 3: "not_implemented",
    4: "invalid_params", 5: "busy", 6: "no_contact", 7: "no_channel",
    8: "buffer_full", 9: "msg_too_long", 10: "no_key",
}


# ── Raw TCP Connection ────────────────────────────────────────────────────

class MeshCoreRawConnection:
    """Raw binary protocol connection to a MeshCore node over TCP.

    Handles framing, send, receive, and response parsing.
    No meshcore_py dependency.
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._cmd_lock = asyncio.Lock()
        self._recv_buffer = b""

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        logger.info("MeshCore(raw): connected to %s:%s", self.host, self.port)

    async def disconnect(self) -> None:
        if self.writer:
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
            except Exception:
                pass
        self.reader = None
        self.writer = None

    @property
    def is_connected(self) -> bool:
        return self.writer is not None

    # ── Frame I/O ────────────────────────────────────────────────────────

    async def _read_exactly(self, n: int) -> bytes:
        """Read exactly n bytes from the stream."""
        data = b""
        while len(data) < n:
            chunk = await asyncio.wait_for(self.reader.read(n - len(data)), timeout=15.0)
            if not chunk:
                raise ConnectionError("TCP connection closed")
            data += chunk
        return data

    async def read_frame(self) -> Tuple[int, bytes]:
        """Read one frame: returns (packet_type, payload_bytes).

        Frame format: 0x3E + 2-byte LE size + payload
        """
        while True:
            # Find frame marker, collecting junk bytes for batch logging
            junk_buf = bytearray()
            while True:
                b = await self._read_exactly(1)
                if b[0] == FRAME_RECV_MARKER:
                    break
                junk_buf.append(b[0])
            
            if junk_buf:
                # Log readable chunks (hex + ascii) for debug analysis
                if len(junk_buf) > 3 or any(c < 0x20 or c > 0x7E for c in junk_buf):
                    # Binary-looking junk — hex dump
                    logger.info("MeshCore(raw): %d junk bytes: %s", len(junk_buf), bytes(junk_buf).hex())
                else:
                    # Looks like ASCII debug output
                    text = bytes(junk_buf).decode("ascii", "replace").rstrip()
                    if text.strip():
                        logger.info("MeshCore(raw): firmware debug: %s", text.strip())

            # Read size (2 bytes LE)
            size_bytes = await self._read_exactly(2)
            size = int.from_bytes(size_bytes, "little")

            if size > MAX_FRAME_SIZE:
                logger.warning("MeshCore(raw): invalid frame size %d, skipping", size)
                continue

            # Read payload
            payload = await self._read_exactly(size)

            if len(payload) < 1:
                logger.warning("MeshCore(raw): empty payload")
                continue

            pkt_type = payload[0]
            return pkt_type, payload[1:]

    async def send_frame(self, payload: bytes) -> None:
        """Send one frame: 0x3C + 2-byte LE size + payload."""
        size = len(payload)
        frame = bytes([FRAME_SEND_MARKER]) + size.to_bytes(2, "little") + payload
        self.writer.write(frame)
        await self.writer.drain()

    # ── Command dispatch ─────────────────────────────────────────────────

    async def send_command(self, cmd_bytes: bytes,
                           expected_types: List[int],
                           timeout: float = 15.0) -> Tuple[int, bytes]:
        """Send a command and wait for one of the expected response types.

        Returns (packet_type, payload_bytes).
        Uses the command lock to serialize all TCP traffic.
        """
        async with self._cmd_lock:
            await self.send_frame(cmd_bytes)
            logger.debug("MeshCore(raw): send_command sent %s, waiting for %s",
                         cmd_bytes.hex()[:20], [hex(t) for t in expected_types])
            deadline = time.time() + timeout
            while time.time() < deadline:
                pkt_type, payload = await self.read_frame()
                logger.debug("MeshCore(raw): send_command recv type=0x%02x payload=%s",
                             pkt_type, payload[:20].hex())
                if pkt_type in expected_types:
                    logger.debug("MeshCore(raw): send_command MATCH type=0x%02x", pkt_type)
                    return pkt_type, payload
                # Unsolicited message (e.g. push notification) — handle it
                await self._handle_unsolicited(pkt_type, payload)
            logger.warning("MeshCore(raw): send_command TIMEOUT after %.1fs", timeout)
            # Drain stale frames from TCP stream — the node's response may
            # arrive after our deadline, poisoning the next command's read.
            try:
                drained = 0
                while True:
                    pkt_type, payload = await asyncio.wait_for(
                        self.read_frame(), timeout=0.3)
                    logger.debug("MeshCore(raw): drain stale type=0x%02x", pkt_type)
                    await self._handle_unsolicited(pkt_type, payload)
                    drained += 1
            except (asyncio.TimeoutError, ConnectionError, Exception):
                pass
            if drained:
                logger.debug("MeshCore(raw): drained %d stale frames after timeout", drained)
            return PKT_ERROR, b"\x01"  # timeout → generic error

    async def send_and_wait_ack(self, cmd_bytes: bytes,
                                 timeout: float = 15.0) -> Tuple[int, bytes]:
        """Send a command, get MSG_SENT, then hold the lock and wait for
        the matching ACK. Returns (PKT_OK, b'') on success or
        (PKT_ERROR, reason_bytes) on failure.

        The lock is held for the ENTIRE send+ACK cycle — no other command
        (poll, keepalive, another send) can steal the ACK frame.
        """
        async with self._cmd_lock:
            await self.send_frame(cmd_bytes)
            deadline = time.time() + timeout
            # Phase 1: wait for MSG_SENT
            while time.time() < deadline:
                pkt_type, payload = await self.read_frame()
                if pkt_type == PKT_MSG_SENT:
                    sent = self.parse_msg_sent(payload)
                    exp_ack = sent["expected_ack"]
                    ack_timeout = max(sent["suggested_timeout"] / 1000 * 1.2, 5.0)
                    ack_deadline = time.time() + ack_timeout
                    # Phase 2: wait for matching ACK (still holding lock)
                    while time.time() < ack_deadline:
                        pkt_type, payload = await self.read_frame()
                        if pkt_type == PKT_ACK:
                            ack_code = payload.hex() if payload else ""
                            if ack_code == exp_ack:
                                return PKT_OK, b""
                        elif pkt_type in (PKT_CONTACT_MSG_RECV, PKT_CONTACT_MSG_RECV_V3,
                                          PKT_CHANNEL_MSG_RECV, PKT_CHANNEL_MSG_RECV_V3):
                            await self._handle_unsolicited(pkt_type, payload)
                    return PKT_ERROR, b"no_ack_received"
                elif pkt_type == PKT_ERROR:
                    return PKT_ERROR, payload
                else:
                    await self._handle_unsolicited(pkt_type, payload)
            return PKT_ERROR, b"timeout"

    async def _handle_unsolicited(self, pkt_type: int, payload: bytes) -> None:
        """Handle unsolicited messages that arrive during command waits.

        These are push notifications (ACK, MESSAGES_WAITING, ADVERTISEMENT)
        or messages that arrived while we were waiting for a command response.
        Subclasses override this to route messages to handlers.
        """
        pass

    # ── Response parsers ─────────────────────────────────────────────────

    @staticmethod
    def parse_self_info(payload: bytes) -> dict:
        """Parse SELF_INFO (0x05) response."""
        buf = io.BytesIO(payload)
        info = {}
        info["adv_type"] = buf.read(1)[0]
        info["tx_power"] = buf.read(1)[0]
        info["max_tx_power"] = buf.read(1)[0]
        info["public_key"] = buf.read(32).hex()
        info["adv_lat"] = int.from_bytes(buf.read(4), "little", signed=True) / 1e6
        info["adv_lon"] = int.from_bytes(buf.read(4), "little", signed=True) / 1e6
        info["multi_acks"] = buf.read(1)[0]
        info["adv_loc_policy"] = buf.read(1)[0]
        tm = buf.read(1)[0]
        info["telemetry_mode_env"] = (tm >> 4) & 0b11
        info["telemetry_mode_loc"] = (tm >> 2) & 0b11
        info["telemetry_mode_base"] = tm & 0b11
        info["manual_add_contacts"] = buf.read(1)[0] > 0
        info["radio_freq"] = int.from_bytes(buf.read(4), "little") / 1000
        info["radio_bw"] = int.from_bytes(buf.read(4), "little") / 1000
        info["radio_sf"] = buf.read(1)[0]
        info["radio_cr"] = buf.read(1)[0]
        info["name"] = buf.read().decode("utf-8", "ignore").replace("\x00", "")
        return info

    @staticmethod
    def parse_device_info(payload: bytes) -> dict:
        """Parse DEVICE_INFO (0x0D) response."""
        buf = io.BytesIO(payload)
        info = {}
        fw_ver = buf.read(1)[0]
        info["fw_ver"] = fw_ver
        if fw_ver >= 3:
            info["max_contacts"] = buf.read(1)[0] * 2
            info["max_channels"] = buf.read(1)[0]
            info["ble_pin"] = int.from_bytes(buf.read(4), "little")
            info["fw_build"] = buf.read(12).decode("utf-8", "ignore").replace("\x00", "")
            info["model"] = buf.read(40).decode("utf-8", "ignore").replace("\x00", "")
            info["ver"] = buf.read(20).decode("utf-8", "ignore").replace("\x00", "")
        if fw_ver >= 9:
            rpt = buf.read(1)
            if len(rpt) > 0:
                info["repeat"] = (rpt[0] != 0)
        if fw_ver >= 10:
            info["path_hash_mode"] = buf.read(1)[0]
        return info

    @staticmethod
    def parse_battery(payload: bytes) -> dict:
        """Parse BATTERY (0x0C) response."""
        if len(payload) < 2:
            return {"level": 0}
        level = int.from_bytes(payload[:2], "little")
        result = {"level": level}
        if len(payload) >= 10:
            result["used_kb"] = int.from_bytes(payload[2:6], "little")
            result["total_kb"] = int.from_bytes(payload[6:10], "little")
        return result

    @staticmethod
    def parse_msg_sent(payload: bytes) -> dict:
        """Parse MSG_SENT (0x06) response."""
        buf = io.BytesIO(payload)
        return {
            "type": buf.read(1)[0],
            "expected_ack": buf.read(4).hex(),
            "suggested_timeout": int.from_bytes(buf.read(4), "little"),
        }

    @staticmethod
    def parse_contact_msg(payload: bytes, is_v3: bool = False) -> dict:
        """Parse CONTACT_MSG_RECV (0x07) or V3 (0x10).

        Firmware format (from companion protocol docs):
          Standard: pubkey_prefix(6) + path_len(1) + txt_type(1) + timestamp(4) + [signature(4) if txt_type==2] + text
          V3:       SNR(1) + reserved(2) + pubkey_prefix(6) + path_len(1) + txt_type(1) + timestamp(4) + [signature(4) if txt_type==2] + text

        The path_len byte is just the hop count (or 0xFF for direct) — NO path hash
        bytes follow it.  Same bug as parse_channel_msg: the old code read phantom
        path bytes that consumed txt_type, timestamp, and the start of the message.
        """
        buf = io.BytesIO(payload)
        msg = {"type": "PRIV"}
        if is_v3:
            msg["SNR"] = int.from_bytes(buf.read(1), "little", signed=True) / 4
            buf.read(2)  # reserved
        msg["pubkey_prefix"] = buf.read(6).hex()
        path_len = buf.read(1)[0]
        if path_len == 255:
            msg["path_len"] = 255
            msg["path"] = ""
        else:
            msg["path_len"] = path_len
            msg["path"] = ""  # firmware does not include path hash bytes in this response
        msg["txt_type"] = buf.read(1)[0]
        msg["sender_timestamp"] = int.from_bytes(buf.read(4), "little")
        if msg["txt_type"] == 2:
            msg["signature"] = buf.read(4).hex()
        msg["text"] = buf.read().decode("utf-8", "ignore")
        return msg

    @staticmethod
    def parse_channel_msg(payload: bytes, is_v3: bool = False) -> dict:
        """Parse CHANNEL_MSG_RECV (0x08) or V3 (0x11).

        Firmware format (from companion protocol docs):
          Standard: channel_idx(1) + path_len(1) + txt_type(1) + timestamp(4) + text
          V3:       SNR(1) + reserved(2) + channel_idx(1) + path_len(1) + txt_type(1) + timestamp(4) + text

        The path_len byte is just the hop count (or 0xFF for direct) — NO path hash
        bytes follow it.  The old code incorrectly read path_len × hash_size bytes
        as path data, consuming txt_type, timestamp, and the start of the message
        text (butchering sender names like ADL-HANDHELD → HELD/NDHELD).
        """
        buf = io.BytesIO(payload)
        msg = {"type": "CHAN"}
        if is_v3:
            msg["SNR"] = int.from_bytes(buf.read(1), "little", signed=True) / 4
            buf.read(2)  # reserved
        msg["channel_idx"] = buf.read(1)[0]
        path_len = buf.read(1)[0]
        if path_len == 255:
            msg["path_len"] = 255
            msg["path"] = ""
        else:
            msg["path_len"] = path_len
            msg["path"] = ""  # firmware does not include path hash bytes in this response
        msg["txt_type"] = buf.read(1)[0]
        msg["sender_timestamp"] = int.from_bytes(buf.read(4), "little", signed=False)
        msg["text"] = buf.read().decode("utf-8", "ignore")
        return msg

    @staticmethod
    def parse_contact(payload: bytes) -> dict:
        """Parse CONTACT (0x03) entry."""
        buf = io.BytesIO(payload)
        c = {}
        c["public_key"] = buf.read(32).hex()
        c["type"] = buf.read(1)[0]
        c["flags"] = buf.read(1)[0]
        plen = buf.read(1)[0]
        if plen == 255:
            c["out_path_hash_mode"] = -1
            c["out_path_len"] = -1
        else:
            c["out_path_hash_mode"] = plen >> 6
            c["out_path_len"] = plen & 0x3F
        c["out_path"] = buf.read(64).replace(b"\x00", b"").hex()
        c["adv_name"] = buf.read(32).decode("utf-8", "ignore").replace("\x00", "")
        c["last_advert"] = int.from_bytes(buf.read(4), "little")
        c["adv_lat"] = int.from_bytes(buf.read(4), "little", signed=True) / 1e6
        c["adv_lon"] = int.from_bytes(buf.read(4), "little", signed=True) / 1e6
        c["lastmod"] = int.from_bytes(buf.read(4), "little")
        return c

    @staticmethod
    def parse_channel_info(payload: bytes) -> dict:
        """Parse CHANNEL_INFO (0x12) response.

        Format: 1-byte channel_idx + null-terminated name (max 32 bytes)
        + 16-byte channel secret.
        """
        buf = io.BytesIO(payload)
        info = {"channel_idx": buf.read(1)[0]}
        # Read name until null byte (max 32 bytes)
        name_bytes = b""
        for _ in range(32):
            b = buf.read(1)
            if not b or b == b"\x00":
                break
            name_bytes += b
        info["channel_name"] = name_bytes.decode("utf-8", "ignore")
        return info

    @staticmethod
    def parse_stats(payload: bytes) -> dict:
        """Parse STATS (0x18) response."""
        if len(payload) < 1:
            return {}
        stats_type = payload[0]
        data = payload[1:]
        if stats_type == 0:  # core
            if len(data) >= 9:
                battery_mv, uptime, errors, queue_len = struct.unpack('<H I H B', data[:9])
                return {"battery_mv": battery_mv, "uptime_secs": uptime,
                        "errors": errors, "queue_len": queue_len}
        elif stats_type == 1:  # radio
            if len(data) >= 12:
                noise, rssi, snr_scaled, tx_air, rx_air = struct.unpack('<h b b I I', data[:12])
                return {"noise_floor": noise, "last_rssi": rssi,
                        "last_snr": snr_scaled / 4.0, "tx_air_secs": tx_air,
                        "rx_air_secs": rx_air}
        elif stats_type == 2:  # packets
            if len(data) >= 24:
                recv, sent, flood_tx, direct_tx, flood_rx, direct_rx = \
                    struct.unpack('<I I I I I I', data[:24])
                result = {"recv": recv, "sent": sent, "flood_tx": flood_tx,
                          "direct_tx": direct_tx, "flood_rx": flood_rx,
                          "direct_rx": direct_rx}
                if len(data) >= 28:
                    result["recv_errors"] = struct.unpack('<I', data[24:28])[0]
                return result
        return {}


# ── MeshCore Adapter ──────────────────────────────────────────────────────

class MeshCoreAdapter(BasePlatformAdapter):
    """MeshCore adapter using raw binary protocol — no meshcore_py."""

    # Singleton ref for tool handlers (set in connect(), cleared in disconnect())
    _instance: Optional["MeshCoreAdapter"] = None

    def __init__(self, config, **kwargs):
        platform = Platform("meshcore")
        super().__init__(config=config, platform=platform)
        extra = getattr(config, "extra", {}) or {}

        self.host = os.getenv("MESHCORE_HOST") or extra.get("host", "")
        self.port = int(os.getenv("MESHCORE_PORT") or extra.get("port", 5000))
        self.bot_name = os.getenv("MESHCORE_BOT_NAME") or extra.get("bot_name", "meshcore-bot")  # fallback

        # Packet-level debug logging — set MESHCORE_DEBUG=true to see every frame
        debug_raw = os.getenv("MESHCORE_DEBUG") or extra.get("debug", "")
        self.debug_enabled = debug_raw.lower() in {"1", "true", "yes"}
        if self.debug_enabled:
            logger.setLevel(logging.INFO)
            logger.info("MeshCore: packet debugging ENABLED — all send/recv frames will be logged")

        admin_raw = os.getenv("MESHCORE_ADMIN_NODES") or extra.get("admin_nodes", "")
        self.admin_nodes: Set[str] = {n.strip() for n in admin_raw.split(",") if n.strip()}

        channels_raw = os.getenv("MESHCORE_MONITOR_CHANNELS") or extra.get("monitor_channels", "")
        self.monitor_channels: Optional[Set[int]] = None
        if channels_raw.strip():
            self.monitor_channels = {int(c.strip()) for c in channels_raw.split(",") if c.strip().isdigit()}

        enable_dms = os.getenv("MESHCORE_ENABLE_DMS") or extra.get("enable_dms", "true")
        self.enable_dms = enable_dms.lower() in {"1", "true", "yes"}

        require_mention_raw = os.getenv("MESHCORE_REQUIRE_MENTION") or extra.get("require_mention", "")
        # Per-channel: comma-separated channel indexes that require @mention.
        # Empty = all channels free-for-all. "true"/"1" = all channels require mention (legacy).
        if require_mention_raw.lower() in {"true", "1", "yes"}:
            self.require_mention_channels: Optional[Set[int]] = None  # None = all channels
        elif require_mention_raw.strip():
            self.require_mention_channels = {int(c.strip()) for c in require_mention_raw.split(",") if c.strip().lstrip("-").isdigit()}
        else:
            self.require_mention_channels = set()  # empty set = no channels require mention

        admin_channels_raw = os.getenv("MESHCORE_ADMIN_CHANNELS") or extra.get("admin_channels", "")
        self.admin_channels: Set[int] = {int(c.strip()) for c in admin_channels_raw.split(",") if c.strip().isdigit()}

        allowed_raw = os.getenv("MESHCORE_ALLOWED_USERS") or extra.get("allowed_users", "")
        self.allowed_users: Set[str] = {u.strip() for u in allowed_raw.split(",") if u.strip()}
        self.allow_all: bool = os.getenv("MESHCORE_ALLOW_ALL_USERS", "").lower() == "true"

        self._conn: Optional[MeshCoreRawConnection] = None
        self._contacts: Dict[str, dict] = {}
        self._discovered_channels: Set[int] = set()
        self._channel_names: Dict[int, str] = {}  # channel index → name
        self._path_hash_size: int = 1
        self._self_info: dict = {}
        self._own_pubkey_prefix: str = ""  # 6-byte hex prefix of our own public key
        self._poll_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._last_message_time: float = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None
        self._stats_refresh_task: Optional[asyncio.Task] = None
        self._stats_cache: Dict[str, Any] = {}
        self._admin_query_lock: asyncio.Lock = asyncio.Lock()  # prevents poll/keepalive during admin queries
        # Admin query response capture: when set, _route_unsolicited captures
        # DMs from this pubkey prefix into _admin_query_responses instead of
        # routing them to the agent. Prevents send_command's unsolicited handler
        # from stealing repeater CLI responses before the admin query sees them.
        self._admin_query_target: str = ""  # pubkey prefix to capture
        self._admin_query_responses: List[str] = []  # captured responses
        # Deduplication: prevent double-delivery when a message arrives as
        # unsolicited during a poll command's wait and then again as the
        # poll response. Keys: "dm:{pubkey}:{ts}" / "ch:{idx}:{sender}:{ts}"
        self._seen_messages: Set[str] = set()

    @property
    def name(self) -> str:
        return "MeshCore"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.host:
            logger.error("MeshCore: MESHCORE_HOST must be configured")
            self._set_fatal_error("config_missing", "MESHCORE_HOST must be set", retryable=False)
            return False

        self._conn = MeshCoreRawConnection(self.host, self.port)
        # Override _handle_unsolicited to route messages to our handlers
        self._conn._handle_unsolicited = self._route_unsolicited

        try:
            await self._conn.connect()
        except Exception as e:
            logger.error("MeshCore: connect failed — %s", e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

        # Send APP_START to initialize session
        try:
            pkt_type, payload = await self._conn.send_command(
                b"\x01\x03   hermes",
                [PKT_SELF_INFO, PKT_ERROR],
            )
            if pkt_type == PKT_SELF_INFO:
                self._self_info = MeshCoreRawConnection.parse_self_info(payload)
                # Extract our own 6-byte pubkey prefix for self-message filtering
                full_pubkey = self._self_info.get("public_key", "")
                self._own_pubkey_prefix = full_pubkey[:12] if len(full_pubkey) >= 12 else ""
                node_name = self._self_info.get("name", "")
                if node_name:
                    # Derive bot name from node name (strip emoji/suffixes for @mention matching)
                    import re
                    clean = re.sub(r'[^\w\s]', '', node_name).strip()
                    self.bot_name = clean.split()[0] if clean else self.bot_name
                logger.info("MeshCore: self_info loaded, name=%s, bot_name=%s",
                             node_name, self.bot_name)
            else:
                logger.warning("MeshCore: APP_START returned error: %s", payload.hex())
        except Exception as e:
            logger.warning("MeshCore: APP_START failed: %s", e)

        # Load contacts
        try:
            await self._load_contacts()
        except Exception as e:
            logger.warning("MeshCore: contacts failed: %s", e)

        # Send flood advert
        try:
            await self._conn.send_command(b"\x07\x01", [PKT_OK, PKT_ERROR])
            logger.info("MeshCore: sent flood advert")
        except Exception:
            pass

        # Load channel secrets
        try:
            await self._load_channels()
        except Exception as e:
            logger.warning("MeshCore: channel secrets failed: %s", e)

        # Get path hash size
        try:
            pkt_type, payload = await self._conn.send_command(
                b"\x16\x03", [PKT_DEVICE_INFO, PKT_ERROR])
            if pkt_type == PKT_DEVICE_INFO:
                dev = MeshCoreRawConnection.parse_device_info(payload)
                if "path_hash_mode" in dev:
                    self._path_hash_size = dev["path_hash_mode"] + 1
                    logger.info("MeshCore: path hash size = %d-byte", self._path_hash_size)
        except Exception:
            pass

        self._mark_connected()
        # Set class-level singleton for tool handlers
        MeshCoreAdapter._instance = self
        logger.info("MeshCore: connected (raw protocol), channels=%s, DMs=%s",
                    sorted(self.monitor_channels) if self.monitor_channels else "(discovery)",
                    "on" if self.enable_dms else "off")
        self._start_poll()
        self._start_keepalive()
        self._start_watchdog()
        self._start_stats_refresh()
        return True

    async def disconnect(self) -> None:
        self._stop_stats_refresh()
        self._stop_watchdog()
        self._stop_keepalive()
        self._stop_poll()
        self._mark_disconnected()
        MeshCoreAdapter._instance = None
        if self._conn:
            try:
                await self._conn.disconnect()
            except Exception:
                pass
            self._conn = None
        self._contacts.clear()
        self._discovered_channels.clear()
        self._channel_names.clear()

    async def _load_contacts(self) -> None:
        """Load all contacts from the node."""
        pkt_type, payload = await self._conn.send_command(
            b"\x04", [PKT_CONTACT_START, PKT_ERROR])
        if pkt_type == PKT_ERROR:
            return

        # CONTACT_START gives us the count
        contact_nb = int.from_bytes(payload[:4], "little") if len(payload) >= 4 else 0

        contacts = {}
        for _ in range(contact_nb):
            pkt_type, payload = await self._conn.read_frame()
            if pkt_type == PKT_CONTACT:
                c = MeshCoreRawConnection.parse_contact(payload)
                contacts[c["public_key"]] = c
            elif pkt_type == PKT_CONTACT_END:
                break

        # Read CONTACT_END
        try:
            pkt_type, payload = await self._conn.read_frame()
        except Exception:
            pass

        self._contacts = contacts
        logger.info("MeshCore: loaded %d contacts", len(contacts))

    async def _load_channels(self) -> None:
        """Load channel secrets for channels 0-3 + monitored channels."""
        channels_to_load = set(self.monitor_channels or [])
        for i in range(4):
            channels_to_load.add(i)
        for idx in sorted(channels_to_load):
            try:
                pkt_type, payload = await self._conn.send_command(
                    bytes([CMD_GET_CHANNEL, idx]),
                    [PKT_CHANNEL_INFO, PKT_ERROR])
                if pkt_type == PKT_CHANNEL_INFO:
                    ch = MeshCoreRawConnection.parse_channel_info(payload)
                    name = ch.get("channel_name", "")
                    self._discovered_channels.add(idx)
                    if name:
                        self._channel_names[idx] = name
                        logger.info("MeshCore: loaded channel %d: %s", idx, name)
            except Exception as e:
                logger.debug("MeshCore: channel %d load failed: %s", idx, e)

    # ── Unsolicited message routing ────────────────────────────────────────

    async def _route_unsolicited(self, pkt_type: int, payload: bytes) -> None:
        """Route unsolicited messages (push notifications, incoming messages)
        that arrive during command waits."""
        if pkt_type == PKT_CONTACT_MSG_RECV:
            msg = MeshCoreRawConnection.parse_contact_msg(payload, is_v3=False)
            await self._route_dm(msg)
        elif pkt_type == PKT_CONTACT_MSG_RECV_V3:
            msg = MeshCoreRawConnection.parse_contact_msg(payload, is_v3=True)
            await self._route_dm(msg)
        elif pkt_type == PKT_CHANNEL_MSG_RECV:
            msg = MeshCoreRawConnection.parse_channel_msg(payload, is_v3=False)
            await self._handle_channel_message(msg)
        elif pkt_type == PKT_CHANNEL_MSG_RECV_V3:
            msg = MeshCoreRawConnection.parse_channel_msg(payload, is_v3=True)
            await self._handle_channel_message(msg)
        elif pkt_type == PKT_NEW_ADVERT:
            c = MeshCoreRawConnection.parse_contact(payload)
            pubkey = c.get("public_key", "")
            if pubkey:
                self._contacts[pubkey] = c
                logger.info("MeshCore: new contact: %s", c.get("adv_name", pubkey[:12]))
        elif pkt_type in (PKT_BINARY_RESPONSE, PKT_STATUS_RESPONSE, PKT_TELEMETRY_RESPONSE):
            # Binary response push — capture for admin query if active
            if self._admin_query_target:
                logger.debug("MeshCore: admin query CAPTURED binary push type=0x%02x len=%d",
                             pkt_type, len(payload) if payload else 0)
                if pkt_type == PKT_STATUS_RESPONSE:
                    parsed = self._parse_status_response(payload, self._admin_query_target)
                elif pkt_type == PKT_BINARY_RESPONSE:
                    parsed = self._parse_binary_response(payload, self._admin_query_target)
                else:
                    parsed = f"[TELEMETRY] {payload.hex() if payload else 'empty'}"
                self._admin_query_responses.append(parsed)
            else:
                logger.debug("MeshCore: binary push type=0x%02x (no admin query active)", pkt_type)
        # ACK, MESSAGES_WAITING, ADVERTISEMENT — silently consumed

    async def _route_dm(self, msg: dict) -> None:
        """Route a DM: capture for admin query if target matches, else handle normally."""
        pubkey_prefix = msg.get("pubkey_prefix", "")
        if self._admin_query_target and pubkey_prefix.lower() == self._admin_query_target.lower():
            text = msg.get("text", "")
            logger.debug("MeshCore: admin query CAPTURED response from %s: %s",
                         pubkey_prefix, text[:80] if text else "(empty)")
            if text:
                self._admin_query_responses.append(text)
            return
        await self._handle_direct_message(msg)

    # ── Poll loop ─────────────────────────────────────────────────────────

    def _start_poll(self):
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    def _stop_poll(self):
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = None

    async def _poll_loop(self):
        """Poll for messages every 2 seconds using CMD_SYNC_NEXT_MESSAGE."""
        while self._conn and self._conn.is_connected:
            # Skip polling during admin queries — they hold the cmd lock
            if self._admin_query_lock.locked():
                await asyncio.sleep(2.0)
                continue
            try:
                pkt_type, payload = await self._conn.send_command(
                    b"\x0A",
                    [PKT_CONTACT_MSG_RECV, PKT_CONTACT_MSG_RECV_V3,
                     PKT_CHANNEL_MSG_RECV, PKT_CHANNEL_MSG_RECV_V3,
                     PKT_NO_MORE_MSGS, PKT_ERROR],
                    timeout=5.0,
                )
                if pkt_type == PKT_CONTACT_MSG_RECV:
                    msg = MeshCoreRawConnection.parse_contact_msg(payload, is_v3=False)
                    await self._handle_direct_message(msg)
                elif pkt_type == PKT_CONTACT_MSG_RECV_V3:
                    msg = MeshCoreRawConnection.parse_contact_msg(payload, is_v3=True)
                    await self._handle_direct_message(msg)
                elif pkt_type == PKT_CHANNEL_MSG_RECV:
                    msg = MeshCoreRawConnection.parse_channel_msg(payload, is_v3=False)
                    await self._handle_channel_message(msg)
                elif pkt_type == PKT_CHANNEL_MSG_RECV_V3:
                    msg = MeshCoreRawConnection.parse_channel_msg(payload, is_v3=True)
                    await self._handle_channel_message(msg)
                # NO_MORE_MSGS or ERROR → sleep then retry
            except Exception as e:
                logger.debug("MeshCore: poll error (non-fatal): %s", e)
            await asyncio.sleep(2.0)

    # ── Keepalive ─────────────────────────────────────────────────────────

    def _start_keepalive(self):
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _stop_keepalive(self):
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    async def _keepalive_loop(self):
        """get_bat() every 15s — keeps TCP pipe alive. Triggers reconnect on failure."""
        failures = 0
        while self._conn and self._conn.is_connected:
            await asyncio.sleep(15)
            if not self._conn or not self._conn.is_connected:
                return
            # Skip keepalive during admin queries — they hold the cmd lock
            if self._admin_query_lock.locked():
                continue
            try:
                await self._conn.send_command(b"\x14", [PKT_BATTERY, PKT_ERROR], timeout=5.0)
                failures = 0
                # Write state file for dashboard after successful keepalive
                self._write_state_file()
                # Check for pending admin requests from dashboard
                await self._process_admin_request()
            except Exception as e:
                failures += 1
                logger.warning("MeshCore: keepalive failed (%d): %s", failures, e)
                if failures >= 2:
                    logger.warning("MeshCore: keepalive — reconnecting after %d failures", failures)
                    await self._reconnect()
                    return

    # ── State file for dashboard ──────────────────────────────────────────

    STATE_FILE = "/tmp/hermes-meshcore-state.json"
    ADMIN_REQUEST_FILE = "/tmp/hermes-meshcore-admin-request.json"
    ADMIN_RESPONSE_FILE = "/tmp/hermes-meshcore-admin-response.json"

    def _write_state_file(self):
        """Write current adapter state to a shared JSON file for the dashboard API."""
        try:
            state = {
                "connected": self._conn is not None and self._conn.is_connected,
                "host": self.host,
                "port": self.port,
                "node": self._build_node_info(),
                "stats": self._build_stats_info(),
                "contacts": self._build_contacts_info(),
                "known_nodes": self._build_known_nodes(),
                "channels": sorted(self._discovered_channels) if self._discovered_channels else [],
                "channel_names": {str(idx): name for idx, name in self._channel_names.items()},
                "last_message_ago_s": round(time.time() - self._last_message_time, 1) if self._last_message_time else None,
                "last_message_time": self._last_message_time if self._last_message_time else None,
                "dms_enabled": self.enable_dms,
                "admin": {
                    "nodes": sorted(self.admin_nodes) if self.admin_nodes else [],
                    "channels": sorted(self.admin_channels) if self.admin_channels else [],
                    "require_mention_channels": sorted(self.require_mention_channels) if self.require_mention_channels else [],
                    "allow_all_users": self.allow_all,
                    "allowed_users": sorted(self.allowed_users) if self.allowed_users else [],
                },
                "updated_at": time.time(),
            }
            with open(self.STATE_FILE, "w") as f:
                json.dump(state, f)
        except Exception:
            pass  # Non-critical — dashboard will show stale data

    async def _process_admin_request(self):
        """Check for a pending admin request file from the dashboard, process it,
        and write the response. Runs inside the keepalive loop (every 15s)."""
        try:
            if not os.path.exists(self.ADMIN_REQUEST_FILE):
                return
            with open(self.ADMIN_REQUEST_FILE) as f:
                req = json.load(f)
            # Remove request file so we don't re-process it
            os.remove(self.ADMIN_REQUEST_FILE)

            node = req.get("node", "")
            command = req.get("command", "")
            password = req.get("password", "")
            request_id = req.get("request_id", "")

            logger.info("MeshCore: processing admin request %s: %s → %s", request_id, node, command)
            result = await self.query_remote_repeater(node, command, password=password, timeout=90.0)
            result["request_id"] = request_id
            result["completed_at"] = time.time()

            with open(self.ADMIN_RESPONSE_FILE, "w") as f:
                json.dump(result, f)
            logger.debug("MeshCore: admin request %s complete: %s", request_id,
                        "success" if result.get("success") else "failed")
        except Exception as e:
            logger.warning("MeshCore: admin request processing failed: %s", e)

    def _build_node_info(self) -> dict:
        si = self._self_info or {}
        radio = {}
        if si.get("radio_freq"):
            radio["freq_mhz"] = round(si["radio_freq"], 3)
        if si.get("radio_bw"):
            radio["bw_khz"] = round(si["radio_bw"], 1)
        if si.get("radio_sf"):
            radio["sf"] = si["radio_sf"]
        if si.get("radio_cr"):
            radio["cr"] = si["radio_cr"]
        return {
            "name": si.get("name", "unknown"),
            "pubkey_prefix": si.get("public_key", "")[:12] if si.get("public_key") else "",
            "lat": si.get("adv_lat"),
            "lon": si.get("adv_lon"),
            "radio": radio if radio else None,
        }

    def _build_stats_info(self) -> dict:
        s = self._stats_cache or {}
        core = s.get("core", {})
        radio = s.get("radio", {})
        packets = s.get("packets", {})
        return {
            "battery_mv": core.get("battery_mv"),
            "uptime_s": core.get("uptime_secs"),
            "errors": core.get("errors"),
            "queue_len": core.get("queue_len"),
            "noise": radio.get("noise_floor"),
            "rssi": radio.get("last_rssi"),
            "snr": radio.get("last_snr"),
            "tx_packets": packets.get("sent"),
            "rx_packets": packets.get("recv"),
        }

    def _build_contacts_info(self) -> dict:
        contacts = self._contacts or {}
        return {
            "total": len(contacts),
            "repeaters": sum(1 for c in contacts.values() if c.get("type") == 2),
            "clients": sum(1 for c in contacts.values() if c.get("type") == 1),
            "rooms": sum(1 for c in contacts.values() if c.get("type") == 3),
        }

    def _build_known_nodes(self) -> list:
        """Return list of known repeater nodes for the admin query dropdown."""
        contacts = self._contacts or {}
        nodes = []
        for c in contacts.values():
            if c.get("type") == 2:  # repeater
                nodes.append({
                    "name": c.get("adv_name", ""),
                    "pubkey_prefix": c.get("public_key", "")[:12] if c.get("public_key") else "",
                    "lat": c.get("adv_lat"),
                    "lon": c.get("adv_lon"),
                    "out_path_len": c.get("out_path_len"),
                })
        nodes.sort(key=lambda n: n.get("name", ""))
        return nodes

    # ── Watchdog ──────────────────────────────────────────────────────────

    def _start_watchdog(self):
        if self._watchdog_task is None or self._watchdog_task.done():
            self._last_message_time = time.time()
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    def _stop_watchdog(self):
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def _watchdog_loop(self):
        """Reconnect if no messages for 120 seconds."""
        while self._conn and self._conn.is_connected:
            await asyncio.sleep(30)
            if not self._conn or not self._conn.is_connected:
                return
            if time.time() - self._last_message_time > 120 and self._last_message_time > 0:
                logger.warning("MeshCore: watchdog — reconnecting")
                await self._reconnect()
                return

    async def _reconnect(self):
        """Tear down and reconnect. Used by keepalive and watchdog."""
        self._stop_keepalive()
        self._stop_poll()
        self._stop_stats_refresh()
        self._mark_disconnected()
        if self._conn:
            try:
                await self._conn.disconnect()
            except Exception:
                pass
            self._conn = None
        self._contacts.clear()
        try:
            await self.connect()
        except Exception as e:
            logger.error("MeshCore: reconnect error: %s", e)

    # ── Stats refresh ──────────────────────────────────────────────────────

    def _start_stats_refresh(self):
        if self._stats_refresh_task is None or self._stats_refresh_task.done():
            self._stats_refresh_task = asyncio.create_task(self._stats_refresh_loop())

    def _stop_stats_refresh(self):
        if self._stats_refresh_task and not self._stats_refresh_task.done():
            self._stats_refresh_task.cancel()
        self._stats_refresh_task = None

    async def _stats_refresh_loop(self):
        """Refresh stats cache immediately, then every 5 minutes."""
        # Immediate first fetch
        try:
            self._stats_cache = await self.get_stats()
        except Exception as e:
            logger.debug("MeshCore: initial stats fetch error: %s", e)
        while self._conn and self._conn.is_connected:
            await asyncio.sleep(300)
            if not self._conn or not self._conn.is_connected:
                return
            try:
                self._stats_cache = await self.get_stats()
            except Exception as e:
                logger.debug("MeshCore: stats refresh error: %s", e)

    def _stats_context(self) -> str:
        """Build a compact stats string for platform context injection."""
        if not self._stats_cache:
            return ""
        parts = []
        core = self._stats_cache.get("core", {})
        if core:
            parts.append(f"battery={core.get('battery_mv', '?')}mV "
                         f"uptime={core.get('uptime_s', '?')}s "
                         f"errors={core.get('errors', '?')}")
        radio = self._stats_cache.get("radio", {})
        if radio:
            parts.append(f"noise={radio.get('noise_floor_dbm', '?')}dBm "
                         f"lastRSSI={radio.get('last_rssi_dbm', '?')} "
                         f"lastSNR={radio.get('last_snr_db', '?')}dB")
        pkts = self._stats_cache.get("packets", {})
        if pkts:
            parts.append(f"pkts(recv={pkts.get('recv', '?')} "
                         f"sent={pkts.get('sent', '?')} "
                         f"flood={pkts.get('flood_tx', '?')}/{pkts.get('flood_rx', '?')} "
                         f"direct={pkts.get('direct_tx', '?')}/{pkts.get('direct_rx', '?')} "
                         f"errs={pkts.get('recv_errors', '?')})")
        if parts:
            return "NODE: " + "; ".join(parts) + ". "
        return ""

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        if not self._conn or not self._conn.is_connected:
            return SendResult(success=False, error="Not connected")

        # Different limits: DMs = 150, channels = 135
        is_channel = chat_id.startswith("channel:")
        max_len = 135 if is_channel else 150
        marker_len = max_len - 13  # reserve 13 chars for " ... (N/M)"

        raw_chunks = self._split_for_mesh(content, max_len=max_len)
        if len(raw_chunks) > 1:
            marker_aware = self._split_for_mesh(content, max_len=marker_len)
            total = len(marker_aware)
            chunks = [c + (" ..." if i < total - 1 else "") + f" ({i+1}/{total})"
                      for i, c in enumerate(marker_aware)]
        else:
            chunks = raw_chunks

        message_ids, errors = [], []
        for i, chunk in enumerate(chunks):
            try:
                if chat_id.startswith("channel:"):
                    channel_idx = int(chat_id.split(":", 1)[1])
                    result = await self._send_channel_msg(channel_idx, chunk)
                elif chat_id.startswith("dm:"):
                    pubkey_prefix = chat_id.split(":", 1)[1]
                    result = await self._send_dm(pubkey_prefix, chunk)
                else:
                    return SendResult(success=False, error=f"Invalid chat_id: {chat_id}")

                # On first failure, reconnect and retry once — but ONLY for
                # connection-level errors (not timeout strings). Retrying a
                # timeout sends a duplicate packet that the receiving node
                # sees as a corrupted/overlapping transmission.
                if result is not True and i == 0 and not isinstance(result, str):
                    logger.warning("MeshCore: send failed (connection error), reconnecting and retrying")
                    await self._reconnect()
                    if chat_id.startswith("channel:"):
                        channel_idx = int(chat_id.split(":", 1)[1])
                        result = await self._send_channel_msg(channel_idx, chunk)
                    elif chat_id.startswith("dm:"):
                        pubkey_prefix = chat_id.split(":", 1)[1]
                        result = await self._send_dm(pubkey_prefix, chunk)

                if result is True:
                    message_ids.append(str(int(time.time() * 1000)))
                elif isinstance(result, str):
                    errors.append(f"chunk {i+1}: {result}")
                else:
                    errors.append(f"chunk {i+1}: send failed")
                if i < len(chunks) - 1:
                    await asyncio.sleep(1.0)
            except Exception as e:
                errors.append(f"chunk {i+1}: {e}")

        if errors and not message_ids:
            return SendResult(success=False, error="; ".join(errors))
        return SendResult(
            success=True,
            message_id=message_ids[0] if message_ids else "",
            continuation_message_ids=tuple(message_ids[1:]) if len(message_ids) > 1 else (),
        )

    async def _send_channel_msg(self, channel_idx: int, text: str):
        """Send a channel message. Single-shot fire-and-forget — retrying
        on timeout sends a duplicate packet that the receiving node sees as
        a corrupted/overlapping transmission, producing garbled text.
        Returns True on success, error string on failure (message may have
        been delivered despite the error)."""
        timestamp = int(time.time())
        cmd = bytes([CMD_SEND_CHANNEL_TXT_MSG, 0x00, channel_idx]) + \
              timestamp.to_bytes(4, "little") + text.encode("utf-8")
        logger.debug("MeshCore: _send_channel_msg cmd=%s", cmd.hex()[:30])
        try:
            pkt_type, payload = await self._conn.send_command(
                cmd, [PKT_OK, PKT_ERROR], timeout=15.0)
            logger.debug("MeshCore: _send_channel_msg result type=0x%02x payload=%s",
                         pkt_type, payload[:20].hex() if payload else "empty")
            if pkt_type == PKT_OK:
                return True
        except Exception as e:
            logger.debug("MeshCore: _send_channel_msg exception: %s", e)
        return "send_timeout (message may have been delivered)"

    async def _send_dm(self, pubkey_prefix: str, text: str):
        """Send a DM. Single-shot fire-and-forget — retrying on timeout
        sends a duplicate packet that the receiving node sees as a
        corrupted/overlapping transmission, producing garbled text.
        Returns True on MSG_SENT (node accepted), error string on failure
        (message may have been delivered despite the error).

        Does NOT wait for over-the-air ACK — that can take 30+ seconds on LoRa
        and would block the command lock, preventing message reception.
        Fire-and-forget, same as meshcore_py's send_msg().
        """
        contact = self._contacts.get(pubkey_prefix)
        if contact is None:
            prefix_lower = pubkey_prefix.lower()
            for cid, c in self._contacts.items():
                if cid.lower().startswith(prefix_lower):
                    contact = c
                    break
        if contact is None:
            return f"Contact not found: {pubkey_prefix}"

        dst_bytes = bytes.fromhex(contact["public_key"])[:6]
        timestamp = int(time.time())

        cmd = bytes([CMD_SEND_TXT_MSG, 0x00, 0]) + \
              timestamp.to_bytes(4, "little") + dst_bytes + text.encode("utf-8")
        logger.debug("MeshCore: _send_dm cmd=%s", cmd.hex()[:30])
        try:
            pkt_type, payload = await self._conn.send_command(
                cmd, [PKT_MSG_SENT, PKT_ERROR], timeout=10.0)
            logger.debug("MeshCore: _send_dm result type=0x%02x payload=%s",
                         pkt_type, payload[:20].hex() if payload else "empty")
            if pkt_type == PKT_MSG_SENT:
                return True
        except Exception as e:
            logger.debug("MeshCore: _send_dm exception: %s", e)
        return "send_timeout (message may have been delivered)"

    async def send_self_advert(self) -> bool:
        """Send a self-advertisement (flood advert) to announce presence on the mesh.
        Returns True on success."""
        cmd = bytes([CMD_SEND_SELF_ADVERT])
        try:
            pkt_type, payload = await self._conn.send_command(
                cmd, [PKT_OK, PKT_ERROR], timeout=10.0)
            if pkt_type == PKT_OK:
                logger.info("MeshCore: self advert sent successfully")
                return True
            else:
                logger.warning("MeshCore: self advert failed: type=0x%02x", pkt_type)
                return False
        except Exception as e:
            logger.warning("MeshCore: self advert exception: %s", e)
            return False

    @staticmethod
    def _split_for_mesh(text: str, max_len: int = 150) -> list:
        chunks = []
        while len(text) > max_len:
            split_at = text.rfind(" ", 0, max_len)
            if split_at == -1:
                split_at = max_len
            chunks.append(text[:split_at].strip())
            text = text[split_at:].strip()
        if text:
            chunks.append(text)
        return chunks or [""]

    async def send_typing(self, chat_id, metadata=None):
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        if chat_id.startswith("channel:"):
            return {"name": f"Channel {chat_id.split(':',1)[1]}", "type": "group", "chat_id": chat_id}
        elif chat_id.startswith("dm:"):
            pubkey = chat_id.split(":", 1)[1]
            contact = self._contacts.get(pubkey, {})
            return {"name": contact.get("adv_name", pubkey[:8]), "type": "dm", "chat_id": chat_id}
        return {"name": chat_id, "type": "unknown"}

    # ── Message handlers ──────────────────────────────────────────────────

    async def _handle_channel_message(self, msg: dict):
        self._last_message_time = time.time()
        channel_idx = msg.get("channel_idx")
        text = msg.get("text", "")

        # Deduplication: skip if already seen (unsolicited + poll double-delivery)
        ts = msg.get("sender_timestamp", 0)
        sender_name_raw = text.split(":", 1)[0].strip() if ":" in text else "?"
        dedup_key = f"ch:{channel_idx}:{sender_name_raw}:{ts}"
        if dedup_key in self._seen_messages:
            return
        self._seen_messages.add(dedup_key)
        # Prune to last 100 entries when set exceeds 200
        if len(self._seen_messages) > 200:
            self._seen_messages = set(list(self._seen_messages)[-100:])

        logger.info("MeshCore: CHANNEL ch=%s from=%s text=%s",
                    channel_idx, text.split(":",1)[0].strip() if ":" in text else "?", text[:50])

        if channel_idx is not None:
            self._discovered_channels.add(channel_idx)
        if self.monitor_channels is None or channel_idx not in self.monitor_channels:
            return

        # Self-message filter: skip channel messages from our own node (echo of sent messages)
        sender_name_raw = text.split(":", 1)[0].strip() if ":" in text else ""
        node_name = self._self_info.get("name", "") if self._self_info else ""
        if node_name and sender_name_raw == node_name:
            return

        sender_name = "unknown"
        user_prompt = text
        if ":" in text:
            sender_name, user_prompt = text.split(":", 1)
            sender_name = sender_name.strip()
            user_prompt = user_prompt.strip()

        # Per-channel mention gating: None = all channels, set() = none, {1,3} = specific
        mention_required = (self.require_mention_channels is None or
                            channel_idx in self.require_mention_channels)
        if mention_required:
            # MeshCore app sends mentions as @[Full Node Name] (bracketed)
            # Also match plain @name and name: prefixes
            node_name = self._self_info.get("name", "") if self._self_info else ""
            patterns = [
                f"@[{node_name}]", f"@[{node_name.lower()}]",
                f"@{self.bot_name}", f"@{self.bot_name.lower()}",
                self.bot_name + ":", self.bot_name.lower() + ":",
            ]
            for p in patterns:
                if user_prompt.lower().startswith(p.lower()):
                    user_prompt = user_prompt[len(p):].strip()
                    break
            else:
                return

        if not user_prompt:
            return

        # Channel messages use a per-sender user_id so the gateway can
        # distinguish users for [name] prefixing and per-user sessions.
        # Display names are self-reported and can change, but that's
        # acceptable — losing session context on a name change is better
        # than the agent thinking everyone is the same person.
        user_id = f"channel:{channel_idx}:{sender_name}" if channel_idx is not None else f"channel:unknown:{sender_name}"
        await self._dispatch_message(
            text=user_prompt, chat_id=f"channel:{channel_idx}", chat_type="group",
            user_id=user_id, user_name=sender_name, is_admin=False,
            channel_idx=channel_idx,
            metadata={"rssi": msg.get("RSSI"), "snr": msg.get("SNR"),
                      "path_len": msg.get("path_len"), "path": msg.get("path"),
                      "sender_timestamp": msg.get("sender_timestamp"),
                      "attempt": msg.get("attempt")},
        )

    async def _handle_direct_message(self, msg: dict):
        self._last_message_time = time.time()
        if not self.enable_dms:
            return
        text = msg.get("text", "")
        pubkey_prefix = msg.get("pubkey_prefix", "")

        # Deduplication: skip if already seen (unsolicited + poll double-delivery)
        ts = msg.get("sender_timestamp", 0)
        dedup_key = f"dm:{pubkey_prefix}:{ts}"
        if dedup_key in self._seen_messages:
            return
        self._seen_messages.add(dedup_key)

        # Self-message filter: skip DMs from our own node (echo of sent messages)
        if self._own_pubkey_prefix and pubkey_prefix == self._own_pubkey_prefix:
            return
        # Prune to last 100 entries when set exceeds 200
        if len(self._seen_messages) > 200:
            self._seen_messages = set(list(self._seen_messages)[-100:])

        sender_name = "unknown"
        user_prompt = text
        if ":" in text:
            sender_name, user_prompt = text.split(":", 1)
            sender_name = sender_name.strip()
            user_prompt = user_prompt.strip()

        if not user_prompt:
            return

        await self._dispatch_message(
            text=user_prompt, chat_id=f"dm:{pubkey_prefix}", chat_type="dm",
            user_id=pubkey_prefix, user_name=sender_name,
            is_admin=pubkey_prefix in self.admin_nodes,
            metadata={"snr": msg.get("SNR"), "path_len": msg.get("path_len"),
                      "path": msg.get("path"),
                      "sender_timestamp": msg.get("sender_timestamp")},
        )

    async def _dispatch_message(self, text, chat_id, chat_type, user_id, user_name,
                                is_admin=False, channel_idx=None, metadata=None):
        if not self._message_handler:
            return

        source = self.build_source(chat_id=chat_id, chat_name=chat_id,
                                   chat_type=chat_type, user_id=user_id, user_name=user_name)

        security_note = ""
        if chat_type == "group":
            if channel_idx is not None and channel_idx in self.admin_channels:
                security_note = "TRUSTED admin channel. "
            else:
                security_note = (
                    "⚠️ PUBLIC BROADCAST — the sender is NOT the profile user. "
                    "NEVER share personal data (names, emails, addresses, health, "
                    "finances) about the profile user or anyone else. "
                    "If asked 'who am I', answer based on the sender name only — "
                    "do NOT probe memory for the profile user's identity. "
                )
        elif chat_type == "dm":
            if is_admin:
                security_note = "Admin DM. "
            else:
                security_note = (
                    "⚠️ PUBLIC DM — the sender is NOT the profile user. "
                    "NEVER share personal data (names, emails, addresses, health, "
                    "finances) about the profile user or anyone else. "
                    "If asked 'who am I', answer based on the sender name only — "
                    "do NOT probe memory for the profile user's identity. "
                )

        # Different limits: DMs = 150 chars, channels = 135 chars
        char_limit = 135 if chat_type == "group" else 150
        platform_context = (
            f"PLATFORM CONTEXT — MeshCore LoRa mesh: {char_limit} char packets, "
            "auto-split for longer responses. Plain text only, no markdown. "
            "Be concise but COMPLETE — answer the question fully, just use fewer words. "
            "Longer answers are fine; they auto-split into multiple packets. "
            "Messages are prefixed with [sender name] — address the sender by that name. "
            + security_note
        )

        if metadata:
            parts = []
            for key, label in [("rssi", "RSSI"), ("snr", "SNR"), ("path_len", "hops"), ("path", "path")]:
                if metadata.get(key) is not None:
                    parts.append(f"{label}={metadata[key]}")
            if parts:
                platform_context += "RADIO: " + ", ".join(parts) + ". "

        # Inject cached node stats (battery, noise, packet counts)
        stats_ctx = self._stats_context()
        if stats_ctx:
            platform_context += stats_ctx

        event = MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source,
            message_id=str(int(time.time() * 1000)),
            timestamp=__import__("datetime").datetime.now(),
            channel_prompt=platform_context,
        )
        await self.handle_message(event)

    # ── Node management ───────────────────────────────────────────────────

    async def get_node_info(self) -> Dict[str, Any]:
        if not self._conn or not self._conn.is_connected:
            return {"error": "Not connected"}
        info = {"self": self._self_info, "contacts": len(self._contacts),
                "discovered_channels": sorted(self._discovered_channels)}
        try:
            pkt_type, payload = await self._conn.send_command(
                b"\x16\x03", [PKT_DEVICE_INFO, PKT_ERROR])
            if pkt_type == PKT_DEVICE_INFO:
                info["device"] = MeshCoreRawConnection.parse_device_info(payload)
        except Exception as e:
            info["device_error"] = str(e)
        try:
            pkt_type, payload = await self._conn.send_command(
                b"\x14", [PKT_BATTERY, PKT_ERROR])
            if pkt_type == PKT_BATTERY:
                info["battery"] = MeshCoreRawConnection.parse_battery(payload)
        except Exception as e:
            info["battery_error"] = str(e)
        return info

    async def get_channel_info(self, channel_idx: int) -> Dict[str, Any]:
        if not self._conn or not self._conn.is_connected:
            return {"error": "Not connected"}
        try:
            pkt_type, payload = await self._conn.send_command(
                bytes([CMD_GET_CHANNEL, channel_idx]),
                [PKT_CHANNEL_INFO, PKT_ERROR])
            if pkt_type == PKT_CHANNEL_INFO:
                return MeshCoreRawConnection.parse_channel_info(payload)
            return {"error": f"Error: {payload.hex()}"}
        except Exception as e:
            return {"error": str(e)}

    async def set_channel_config(self, channel_idx: int, name: str,
                                  secret_hex: Optional[str] = None) -> Dict[str, Any]:
        if not self._conn or not self._conn.is_connected:
            return {"error": "Not connected"}
        name_bytes = name.encode("utf-8")[:32].ljust(32, b"\x00")
        if secret_hex:
            secret = bytes.fromhex(secret_hex)
        else:
            from hashlib import sha256
            secret = sha256(name.encode("utf-8")).digest()[:16]
        cmd = bytes([CMD_SET_CHANNEL, channel_idx]) + name_bytes + secret
        try:
            pkt_type, _ = await self._conn.send_command(cmd, [PKT_OK, PKT_ERROR])
            if pkt_type == PKT_OK:
                return {"success": True, "channel_idx": channel_idx, "name": name}
            return {"error": "Command failed"}
        except Exception as e:
            return {"error": str(e)}

    async def set_radio_params(self, freq: float, bw: float, sf: int, cr: int) -> Dict[str, Any]:
        if not self._conn or not self._conn.is_connected:
            return {"error": "Not connected"}
        cmd = bytes([CMD_SET_RADIO]) + int(freq * 1000).to_bytes(4, "little") + \
              int(bw * 1000).to_bytes(4, "little") + bytes([sf, cr])
        try:
            pkt_type, _ = await self._conn.send_command(cmd, [PKT_OK, PKT_ERROR])
            return {"success": True} if pkt_type == PKT_OK else {"error": "Command failed"}
        except Exception as e:
            return {"error": str(e)}

    async def set_tx_power(self, power: int) -> Dict[str, Any]:
        if not self._conn or not self._conn.is_connected:
            return {"error": "Not connected"}
        cmd = bytes([CMD_SET_TX_POWER]) + power.to_bytes(4, "little")
        try:
            pkt_type, _ = await self._conn.send_command(cmd, [PKT_OK, PKT_ERROR])
            return {"success": True} if pkt_type == PKT_OK else {"error": "Command failed"}
        except Exception as e:
            return {"error": str(e)}

    async def set_node_name(self, name: str) -> Dict[str, Any]:
        if not self._conn or not self._conn.is_connected:
            return {"error": "Not connected"}
        cmd = bytes([CMD_SET_NAME]) + name.encode("utf-8")
        try:
            pkt_type, _ = await self._conn.send_command(cmd, [PKT_OK, PKT_ERROR])
            return {"success": True} if pkt_type == PKT_OK else {"error": "Command failed"}
        except Exception as e:
            return {"error": str(e)}

    async def set_telemetry_modes(self, base=None, loc=None, env=None) -> Dict[str, Any]:
        if not self._conn or not self._conn.is_connected:
            return {"error": "Not connected"}
        infos = self._self_info.copy()
        if base is not None:
            infos["telemetry_mode_base"] = base
        if loc is not None:
            infos["telemetry_mode_loc"] = loc
        if env is not None:
            infos["telemetry_mode_env"] = env
        tm = (infos["telemetry_mode_base"] & 0b11) | \
             ((infos["telemetry_mode_loc"] & 0b11) << 2) | \
             ((infos["telemetry_mode_env"] & 0b11) << 4)
        cmd = bytes([CMD_SET_OTHER_PARAMS]) + \
              int(infos.get("manual_add_contacts", False)).to_bytes(1, "little") + \
              bytes([tm, infos.get("adv_loc_policy", 0),
                     infos.get("multi_acks", 0)])
        try:
            pkt_type, _ = await self._conn.send_command(cmd, [PKT_OK, PKT_ERROR])
            return {"success": True} if pkt_type == PKT_OK else {"error": "Command failed"}
        except Exception as e:
            return {"error": str(e)}

    async def reboot_node(self) -> Dict[str, Any]:
        if not self._conn or not self._conn.is_connected:
            return {"error": "Not connected"}
        try:
            await self._conn.send_frame(b"\x13reboot")
            return {"success": True, "message": "Reboot command sent"}
        except Exception as e:
            return {"error": str(e)}

    async def get_stats(self) -> Dict[str, Any]:
        if not self._conn or not self._conn.is_connected:
            return {"error": "Not connected"}
        stats = {}
        for stype, name in [(0, "core"), (1, "radio"), (2, "packets")]:
            try:
                pkt_type, payload = await self._conn.send_command(
                    bytes([CMD_GET_STATS, stype]), [PKT_STATS, PKT_ERROR])
                if pkt_type == PKT_STATS:
                    stats[name] = MeshCoreRawConnection.parse_stats(payload)
                else:
                    stats[f"{name}_error"] = payload.hex()
            except Exception as e:
                stats[f"{name}_error"] = str(e)
        return stats

    # ── Remote repeater admin ──────────────────────────────────────────────

    async def _find_contact_by_name(self, name: str) -> Optional[dict]:
        """Fuzzy-search contacts by name substring. Returns the best match."""
        name_lower = name.lower()
        # Exact match first
        for c in self._contacts.values():
            if c.get("adv_name", "").lower() == name_lower:
                return c
        # Substring match
        candidates = []
        for c in self._contacts.values():
            adv = c.get("adv_name", "").lower()
            if name_lower in adv:
                candidates.append((len(adv) - len(name_lower), c))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        # Pubkey prefix match
        for pk, c in self._contacts.items():
            if pk.lower().startswith(name_lower):
                return c
        return None

    @staticmethod
    def _parse_status_response(payload: bytes, pubkey_prefix: str) -> str:
        """Parse a STATUS_RESPONSE (0x87) push frame into human-readable text.

        Format: 8-byte header (tag + timestamp) + status fields.
        Uses the same field layout as meshcore_py's parse_status() with offset=8.
        """
        if len(payload) < 60:
            return f"[STATUS] (too short: {len(payload)} bytes) {payload.hex()}"
        # Fields start at offset 8 (skip 4-byte tag + 4-byte timestamp)
        d = payload
        bat = int.from_bytes(d[8:10], "little")
        tx_queue = int.from_bytes(d[10:12], "little")
        noise = int.from_bytes(d[12:14], "little", signed=True)
        rssi = int.from_bytes(d[14:16], "little", signed=True)
        nb_recv = int.from_bytes(d[16:20], "little")
        nb_sent = int.from_bytes(d[20:24], "little")
        airtime = int.from_bytes(d[24:28], "little")
        uptime = int.from_bytes(d[28:32], "little")
        sent_flood = int.from_bytes(d[32:36], "little")
        sent_direct = int.from_bytes(d[36:40], "little")
        recv_flood = int.from_bytes(d[40:44], "little")
        recv_direct = int.from_bytes(d[44:48], "little")
        full_evts = int.from_bytes(d[48:50], "little")
        snr = int.from_bytes(d[50:52], "little", signed=True) / 4.0
        direct_dups = int.from_bytes(d[52:54], "little")
        flood_dups = int.from_bytes(d[54:56], "little")
        rx_airtime = int.from_bytes(d[56:60], "little")
        recv_errors = int.from_bytes(d[60:64], "little") if len(d) >= 64 else None

        days = uptime // 86400
        hours = (uptime % 86400) // 3600
        mins = (uptime % 3600) // 60
        return (
            f"=== {pubkey_prefix} Status ===\n"
            f"Uptime: {days}d {hours}h {mins}m\n"
            f"Battery: {bat}mV ({bat/1000:.2f}V)\n"
            f"TX Queue: {tx_queue}  |  Full Events: {full_evts}\n"
            f"Noise Floor: {noise}dBm  |  Last RSSI: {rssi}dBm  |  SNR: {snr:.1f}dB\n"
            f"Packets: {nb_recv:,} recv / {nb_sent:,} sent\n"
            f"  Flood: {sent_flood:,} sent / {recv_flood:,} recv\n"
            f"  Direct: {sent_direct:,} sent / {recv_direct:,} recv\n"
            f"  Dups: {direct_dups:,} direct / {flood_dups:,} flood\n"
            f"TX Airtime: {airtime}ms  |  RX Airtime: {rx_airtime}ms\n"
            + (f"Recv Errors: {recv_errors:,}\n" if recv_errors is not None else "")
        )

    @staticmethod
    def _parse_binary_response(payload: bytes, pubkey_prefix: str) -> str:
        """Parse a BINARY_RESPONSE (0x8C) into human-readable text.

        Format: 1 byte skipped + 4-byte tag + response_data.
        For STATUS requests, response_data uses parse_status() with offset=0.
        """
        if len(payload) < 5:
            return f"[BINARY] (too short: {len(payload)} bytes) {payload.hex()}"
        tag = payload[1:5].hex()
        response_data = payload[5:]

        # Try parsing as neighbours (short response, 4+ bytes)
        if 4 <= len(response_data) < 52:
            try:
                return MeshCoreAdapter._parse_neighbours_response(
                    response_data, pubkey_prefix)
            except Exception:
                pass  # Fall through to raw hex
        # Try parsing as status (most common binary response type)
        if len(response_data) >= 52:
            d = response_data
            bat = int.from_bytes(d[0:2], "little")
            tx_queue = int.from_bytes(d[2:4], "little")
            noise = int.from_bytes(d[4:6], "little", signed=True)
            rssi = int.from_bytes(d[6:8], "little", signed=True)
            nb_recv = int.from_bytes(d[8:12], "little")
            nb_sent = int.from_bytes(d[12:16], "little")
            airtime = int.from_bytes(d[16:20], "little")
            uptime = int.from_bytes(d[20:24], "little")
            sent_flood = int.from_bytes(d[24:28], "little")
            sent_direct = int.from_bytes(d[28:32], "little")
            recv_flood = int.from_bytes(d[32:36], "little")
            recv_direct = int.from_bytes(d[36:40], "little")
            full_evts = int.from_bytes(d[40:42], "little")
            snr = int.from_bytes(d[42:44], "little", signed=True) / 4.0
            direct_dups = int.from_bytes(d[44:46], "little")
            flood_dups = int.from_bytes(d[46:48], "little")
            rx_airtime = int.from_bytes(d[48:52], "little")
            recv_errors = int.from_bytes(d[52:56], "little") if len(d) >= 56 else None

            days = uptime // 86400
            hours = (uptime % 86400) // 3600
            mins = (uptime % 3600) // 60
            return (
                f"=== {pubkey_prefix} Status ===\n"
                f"Uptime: {days}d {hours}h {mins}m\n"
                f"Battery: {bat}mV ({bat/1000:.2f}V)\n"
                f"TX Queue: {tx_queue}  |  Full Events: {full_evts}\n"
                f"Noise Floor: {noise}dBm  |  Last RSSI: {rssi}dBm  |  SNR: {snr:.1f}dB\n"
                f"Packets: {nb_recv:,} recv / {nb_sent:,} sent\n"
                f"  Flood: {sent_flood:,} sent / {recv_flood:,} recv\n"
                f"  Direct: {sent_direct:,} sent / {recv_direct:,} recv\n"
                f"  Dups: {direct_dups:,} direct / {flood_dups:,} flood\n"
                f"TX Airtime: {airtime}ms  |  RX Airtime: {rx_airtime}ms\n"
                + (f"Recv Errors: {recv_errors:,}\n" if recv_errors is not None else "")
            )
        # Fallback: raw hex
        return f"[BINARY tag={tag}] {response_data.hex()}"

    @staticmethod
    def _parse_neighbours_response(response_data: bytes, pubkey_prefix: str,
                                    pubkey_prefix_length: int = 4) -> str:
        """Parse a NEIGHBOURS binary response into human-readable text.

        Format: 2B neighbours_count, 2B results_count, then per entry:
          pk_plen bytes pubkey prefix, 4B secs_ago, 1B snr/4
        """
        import io
        bbuf = io.BytesIO(response_data)
        total = int.from_bytes(bbuf.read(2), "little", signed=True)
        count = int.from_bytes(bbuf.read(2), "little", signed=True)
        lines = [f"=== {pubkey_prefix} Neighbours ===",
                 f"Total: {total}  |  In response: {count}"]
        for i in range(count):
            pk = bbuf.read(pubkey_prefix_length).hex()
            secs = int.from_bytes(bbuf.read(4), "little", signed=True)
            snr = int.from_bytes(bbuf.read(1), "little", signed=True) / 4.0
            mins = secs // 60
            lines.append(f"  {i+1}. {pk}  {secs}s ago ({mins}m)  SNR: {snr:.1f}dB")
        return "\n".join(lines)

    async def query_remote_repeater(self, name: str, command: str,
                                     timeout: float = 90.0,
                                     password: str = "") -> Dict[str, Any]:
        """Send a command to a remote repeater using the MeshCore binary protocol.

        Text CLI commands (stats-core, ver, neighbors) only work over serial/UART.
        Over the mesh, repeaters only process binary protocol commands via
        CMD_BINARY_REQ (0x32) or CMD_SEND_ANON_REQ (0x39) with dedicated sub-opcodes.

        Command mapping:
          stats-core, ver, board, get name, get public.key, gps → BINREQ_STATUS
          stats-radio, stats-packets → BINREQ_TELEMETRY
          neighbors → BINREQ_NEIGHBOURS
          clock → ANONREQ_BASIC
          get owner.info → ANONREQ_OWNER
          region list * → ANONREQ_REGIONS
          req_acl → BINREQ_ACL
          Unmapped commands fall back to text DM (will get "Unknown command").

        Returns {"success": True, "responses": [...], "node": {...}} or
        {"success": False, "error": "..."}.
        """
        # Map command names to (opcode, sub_type, extra_data) tuples
        BINARY_COMMAND_MAP = {
            # Binary requests (CMD_BINARY_REQ)
            "stats-core":     (CMD_BINARY_REQ, BINREQ_STATUS, None),
            "stats-radio":    (CMD_BINARY_REQ, BINREQ_TELEMETRY, None),
            "stats-packets":  (CMD_BINARY_REQ, BINREQ_TELEMETRY, None),
            "ver":            (CMD_BINARY_REQ, BINREQ_STATUS, None),
            "board":          (CMD_BINARY_REQ, BINREQ_STATUS, None),
            "neighbors":      (CMD_BINARY_REQ, BINREQ_NEIGHBOURS,
                               b"\x00\xff\x00\x00\x00\x04" + os.urandom(4)),
            "get name":       (CMD_BINARY_REQ, BINREQ_STATUS, None),
            "get public.key": (CMD_BINARY_REQ, BINREQ_STATUS, None),
            "gps":            (CMD_BINARY_REQ, BINREQ_STATUS, None),
            "req_acl":        (CMD_BINARY_REQ, BINREQ_ACL, b"\x00\x00"),
            "req_status":     (CMD_BINARY_REQ, BINREQ_STATUS, None),
            "req_neighbours": (CMD_BINARY_REQ, BINREQ_NEIGHBOURS,
                               b"\x00\xff\x00\x00\x00\x04" + os.urandom(4)),
            "req_telemetry":  (CMD_BINARY_REQ, BINREQ_TELEMETRY, None),
            # Anonymous requests (CMD_SEND_ANON_REQ)
            "clock":           (CMD_SEND_ANON_REQ, ANONREQ_BASIC, None),
            "get owner.info":  (CMD_SEND_ANON_REQ, ANONREQ_OWNER, None),
            "req_owner":       (CMD_SEND_ANON_REQ, ANONREQ_OWNER, None),
            "req_clock":       (CMD_SEND_ANON_REQ, ANONREQ_BASIC, None),
            "region list allowed": (CMD_SEND_ANON_REQ, ANONREQ_REGIONS, None),
            "region list denied":  (CMD_SEND_ANON_REQ, ANONREQ_REGIONS, None),
            "req_regions":     (CMD_SEND_ANON_REQ, ANONREQ_REGIONS, None),
        }

        if not self._conn or not self._conn.is_connected:
            return {"success": False, "error": "Gateway not connected to node"}

        # Hold admin_query_lock so poll/keepalive skip during the query
        async with self._admin_query_lock:
            contact = await self._find_contact_by_name(name)
            if contact is None:
                return {"success": False, "error": f"Node not found: {name}"}

            pubkey = contact["public_key"]
            pubkey_prefix = pubkey[:12]
            full_key = bytes.fromhex(pubkey)  # 32 bytes

            # Set capture target for _route_dm
            self._admin_query_target = pubkey_prefix
            self._admin_query_responses = []
            logger.debug("MeshCore: admin query START target=%s cmd=%s pw=%s",
                        pubkey_prefix, command, "yes" if password else "no")

            try:
                # ── Login (required for all binary commands) ──
                # Always attempt login — even with empty password (guest-level access).
                # Without login, the repeater ignores binary commands.
                login_cmd = bytes([CMD_SEND_LOGIN]) + full_key + password.encode("utf-8")
                logger.debug("MeshCore: admin query sending login: %s", login_cmd.hex()[:40])
                try:
                    pkt_type, payload = await self._conn.send_command(
                        login_cmd, [PKT_MSG_SENT, PKT_ERROR], timeout=10.0)
                    logger.debug("MeshCore: admin query login result: 0x%02x", pkt_type)
                    if pkt_type != PKT_MSG_SENT:
                        return {"success": False, "error": "Login rejected by node"}
                except Exception as e:
                    logger.debug("MeshCore: admin query login exception: %s", e)
                    return {"success": False, "error": f"Login failed: {e}"}

                # Wait for LOGIN_SUCCESS or LOGIN_FAILED
                login_deadline = time.time() + 15.0
                logged_in = False
                while time.time() < login_deadline:
                    try:
                        pkt_type, payload = await self._conn.send_command(
                            b"\x0A",
                            [PKT_LOGIN_SUCCESS, PKT_LOGIN_FAILED,
                             PKT_NO_MORE_MSGS, PKT_ERROR],
                            timeout=5.0,
                        )
                        if pkt_type == PKT_LOGIN_SUCCESS:
                            perms = payload[0] if payload else 0
                            logger.debug("MeshCore: admin query LOGIN SUCCESS perms=%d", perms)
                            logged_in = True
                            break
                        elif pkt_type == PKT_LOGIN_FAILED:
                            logger.debug("MeshCore: admin query LOGIN FAILED")
                            return {"success": False, "error": "Login rejected — wrong password or not authorized"}
                        elif pkt_type == PKT_NO_MORE_MSGS:
                            await asyncio.sleep(2.0)
                            continue
                    except Exception:
                        await asyncio.sleep(2.0)
                if not logged_in:
                    return {"success": False, "error": "Login timed out — no response from repeater"}

                # ── Send the command ──
                mapping = BINARY_COMMAND_MAP.get(command)
                if mapping:
                    cmd_opcode, sub_type, extra_data = mapping
                    # Build binary request: opcode + 32-byte key + sub_type + extra_data
                    cmd_bytes = bytes([cmd_opcode]) + full_key + bytes([sub_type])
                    if extra_data:
                        cmd_bytes += extra_data
                    logger.debug("MeshCore: admin query sending binary req: opcode=0x%02x sub=0x%02x",
                                 cmd_opcode, sub_type)
                else:
                    # Fallback: text DM (will likely get "Unknown command")
                    ts = int(time.time())
                    cmd_bytes = bytes([CMD_SEND_TXT_MSG, 1, 0]) + \
                                ts.to_bytes(4, "little") + full_key[:6] + command.encode("utf-8")
                    logger.debug("MeshCore: admin query sending text cmd (no binary mapping): %s",
                                 cmd_bytes.hex()[:40])

                logger.debug("MeshCore: admin query cmd bytes: %s", cmd_bytes.hex()[:60])
                try:
                    pkt_type, _ = await self._conn.send_command(
                        cmd_bytes, [PKT_MSG_SENT, PKT_ERROR], timeout=10.0)
                    logger.debug("MeshCore: admin query cmd result: 0x%02x", pkt_type)
                    if pkt_type != PKT_MSG_SENT:
                        return {"success": False, "error": "Node rejected send"}
                except Exception as e:
                    logger.debug("MeshCore: admin query cmd exception: %s", e)
                    return {"success": False, "error": f"Send failed: {e}"}

                logger.debug("MeshCore: admin query starting poll, captured_so_far=%d",
                            len(self._admin_query_responses))

                # ── Poll for response ──
                # Binary responses arrive as push notifications:
                #   PKT_STATUS_RESPONSE (0x87) for BINREQ_STATUS
                #   PKT_BINARY_RESPONSE (0x8C) for other binary/anonymous requests
                #   PKT_TELEMETRY_RESPONSE (0x8B) for BINREQ_TELEMETRY
                # Text responses arrive as PKT_CONTACT_MSG_RECV (0x07/0x10)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        pkt_type, payload = await self._conn.send_command(
                            b"\x0A",
                            [PKT_STATUS_RESPONSE, PKT_BINARY_RESPONSE,
                             PKT_TELEMETRY_RESPONSE,
                             PKT_CONTACT_MSG_RECV, PKT_CONTACT_MSG_RECV_V3,
                             PKT_NO_MORE_MSGS, PKT_ERROR],
                            timeout=5.0,
                        )
                        logger.debug("MeshCore: admin poll got type=0x%02x payload=%s",
                                     pkt_type, payload[:40].hex() if payload else "empty")

                        if pkt_type == PKT_STATUS_RESPONSE:
                            # Status response: parse and format
                            logger.debug("MeshCore: admin poll STATUS_RESPONSE len=%d", len(payload) if payload else 0)
                            parsed = self._parse_status_response(payload, pubkey_prefix)
                            self._admin_query_responses.append(parsed)
                            continue
                        elif pkt_type == PKT_BINARY_RESPONSE:
                            logger.debug("MeshCore: admin poll BINARY_RESPONSE len=%d", len(payload) if payload else 0)
                            parsed = self._parse_binary_response(payload, pubkey_prefix)
                            self._admin_query_responses.append(parsed)
                            continue
                        elif pkt_type == PKT_TELEMETRY_RESPONSE:
                            logger.debug("MeshCore: admin poll TELEMETRY_RESPONSE len=%d", len(payload) if payload else 0)
                            self._admin_query_responses.append(
                                f"[TELEMETRY] {payload.hex() if payload else 'empty'}")
                            continue
                        elif pkt_type in (PKT_CONTACT_MSG_RECV, PKT_CONTACT_MSG_RECV_V3):
                            # Text DM response (fallback path)
                            msg = MeshCoreRawConnection.parse_contact_msg(
                                payload, is_v3=(pkt_type == PKT_CONTACT_MSG_RECV_V3))
                            msg_pk = msg.get("pubkey_prefix", "")
                            msg_text = msg.get("text", "")
                            logger.debug("MeshCore: admin poll DM from %s (target=%s) text=%s",
                                         msg_pk, pubkey_prefix, msg_text[:80] if msg_text else "(empty)")
                            if msg_pk.lower() == pubkey_prefix.lower() and msg_text:
                                self._admin_query_responses.append(msg_text)
                            continue
                        elif pkt_type == PKT_NO_MORE_MSGS:
                            await asyncio.sleep(2.0)
                            continue
                        elif pkt_type == PKT_ERROR:
                            break
                    except Exception:
                        await asyncio.sleep(2.0)

                return {
                    "success": True,
                    "responses": list(self._admin_query_responses),
                    "node": {
                        "name": contact.get("adv_name", ""),
                        "pubkey_prefix": pubkey_prefix,
                        "type": contact.get("type"),
                        "lat": contact.get("adv_lat"),
                        "lon": contact.get("adv_lon"),
                        "last_advert": contact.get("last_advert"),
                    },
                }
            finally:
                self._admin_query_target = ""
                self._admin_query_responses = []

    async def get_contact_details(self, name: str) -> Dict[str, Any]:
        """Get full details for a contact by name or pubkey prefix."""
        contact = await self._find_contact_by_name(name)
        if contact is None:
            return {"success": False, "error": f"Node not found: {name}"}

        type_names = {0: "unknown", 1: "client", 2: "repeater", 3: "room"}
        ctype = contact.get("type")
        return {
            "success": True,
            "name": contact.get("adv_name", ""),
            "pubkey": contact.get("public_key", ""),
            "pubkey_prefix": contact.get("public_key", "")[:12],
            "type": ctype,
            "type_name": type_names.get(ctype, "unknown") if ctype is not None else "unknown",
            "flags": contact.get("flags"),
            "out_path_len": contact.get("out_path_len"),
            "out_path_hash_mode": contact.get("out_path_hash_mode"),
            "out_path": contact.get("out_path", ""),
            "lat": contact.get("adv_lat"),
            "lon": contact.get("adv_lon"),
            "last_advert": contact.get("last_advert"),
            "lastmod": contact.get("lastmod"),
        }


# ── Tool handlers (module-level, adapter ref set during connect) ──────────

MESHCORE_ADMIN_SCHEMA = {
    "name": "meshcore_admin",
    "description": (
        "Query a remote MeshCore repeater or node by sending CLI commands over the mesh. "
        "Opens a separate TCP connection so it doesn't interfere with the gateway. "
        "Guest password is blank by default — read-only commands work without auth. "
        "For admin access, provide the repeater's password. "
        "Supported commands: ver, stats-core, stats-radio, stats-packets, get name, "
        "get lat, get lon, get role, get repeat, get guest.password, get owner.info, "
        "neighbors, clock. Use 'all' to run all read-only commands at once."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node": {
                "type": "string",
                "description": "Node name or pubkey prefix to query (e.g. 'SA-VK5RMB-MT', 'Tungkillo', 'RF-Highbury-RPT')"
            },
            "command": {
                "type": "string",
                "description": "CLI command to send (e.g. 'ver', 'stats-core', 'neighbors', 'all' for all read-only commands)"
            },
            "password": {
                "type": "string",
                "description": "Optional admin password for the repeater. If provided, sends 'password <pw>' before the command."
            },
        },
        "required": ["node", "command"],
    },
}

MESHCORE_CONTACT_SCHEMA = {
    "name": "meshcore_contact",
    "description": (
        "Get detailed information about a MeshCore contact/node from the local contact cache. "
        "Returns name, pubkey, type (client/repeater/room), location (lat/lon), "
        "last advert time, and cached routing path. No mesh traffic — instant lookup."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Node name or pubkey prefix to look up (e.g. 'SA-VK5RMB-MT', 'Tungkillo', 'RF-Highbury-RPT')"
            },
        },
        "required": ["name"],
    },
}

ALL_READONLY_COMMANDS = [
    "ver", "stats-core", "stats-radio", "stats-packets",
    "get name", "get lat", "get lon", "get role", "get repeat",
    "get guest.password", "get owner.info", "neighbors", "clock",
]


async def _handle_meshcore_admin(node: str, command: str, password: str = "") -> str:
    """Handler for meshcore_admin tool. Requires the gateway adapter to be
    connected — uses the gateway's existing TCP connection."""
    adapter = MeshCoreAdapter._instance
    if adapter is None:
        return json.dumps({"success": False, "error": "MeshCore gateway not connected — admin tools require the gateway to be running"})

    if command == "all":
        all_results = {}
        for cmd in ALL_READONLY_COMMANDS:
            result = await adapter.query_remote_repeater(node, cmd, timeout=15.0, password=password)
            all_results[cmd] = result.get("responses", []) if result.get("success") else result.get("error", "failed")
        return json.dumps({"success": True, "node": node, "results": all_results})

    result = await adapter.query_remote_repeater(node, command, password=password)
    return json.dumps(result)


async def _handle_meshcore_admin_query(node: str, command: str, password: str = "") -> str:
    """Handler for meshcore_admin_query tool. Uses the file-based request/response
    mechanism — writes a request file that the gateway's keepalive loop picks up,
    then polls for the response. Works from any session, not just the gateway process."""
    import os, json, time

    REQUEST_FILE = "/tmp/hermes-meshcore-admin-request.json"
    RESPONSE_FILE = "/tmp/hermes-meshcore-admin-response.json"

    # Check if a request is already pending
    if os.path.exists(REQUEST_FILE):
        return json.dumps({"success": False, "error": "An admin query is already in progress — wait and retry"})

    # Check gateway is running (state file exists and is fresh)
    STATE_FILE = "/tmp/hermes-meshcore-state.json"
    if not os.path.exists(STATE_FILE):
        return json.dumps({"success": False, "error": "MeshCore gateway not running (no state file)"})
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        if time.time() - state.get("updated_at", 0) > 60:
            return json.dumps({"success": False, "error": "MeshCore gateway state is stale — gateway may be down"})
    except Exception:
        return json.dumps({"success": False, "error": "Cannot read gateway state"})

    # Write request
    request_id = str(int(time.time()))
    request = {
        "request_id": request_id,
        "node": node,
        "command": command,
        "password": password,
        "submitted_at": time.time(),
    }
    with open(REQUEST_FILE, "w") as f:
        json.dump(request, f)

    # Poll for response (up to 60s)
    deadline = time.time() + 60
    while time.time() < deadline:
        await asyncio.sleep(3)
        if not os.path.exists(RESPONSE_FILE):
            continue
        try:
            with open(RESPONSE_FILE) as f:
                result = json.load(f)
            if result.get("request_id") == request_id:
                os.remove(RESPONSE_FILE)
                return json.dumps(result)
        except Exception:
            continue

    # Timeout — clean up
    if os.path.exists(REQUEST_FILE):
        try:
            os.remove(REQUEST_FILE)
        except Exception:
            pass
    return json.dumps({"success": False, "error": "Timed out waiting for response (60s) — node may be unreachable"})


async def _handle_meshcore_contact(name: str) -> str:
    """Handler for meshcore_contact tool. Requires the gateway adapter to be
    connected — reads from the gateway's contact cache."""
    adapter = MeshCoreAdapter._instance
    if adapter is None:
        return json.dumps({"success": False, "error": "MeshCore gateway not connected — contact lookup requires the gateway to be running"})

    result = await adapter.get_contact_details(name)
    return json.dumps(result)


# ── Plugin registration ───────────────────────────────────────────────────

def check_requirements():
    return bool(os.getenv("MESHCORE_HOST", ""))

def validate_config(config):
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("MESHCORE_HOST") or extra.get("host", ""))

def is_connected(config):
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("MESHCORE_HOST") or extra.get("host", ""))

def _env_enablement():
    host = os.getenv("MESHCORE_HOST", "").strip()
    if not host:
        return None
    seed = {"host": host}
    for key in ["MESHCORE_PORT", "MESHCORE_BOT_NAME", "MESHCORE_ADMIN_NODES",
                "MESHCORE_MONITOR_CHANNELS", "MESHCORE_ENABLE_DMS",
                "MESHCORE_REQUIRE_MENTION", "MESHCORE_ALLOWED_USERS"]:
        val = os.getenv(key, "").strip()
        if val:
            name = key.replace("MESHCORE_", "").lower()
            try:
                seed[name] = int(val)
            except ValueError:
                seed[name] = val
    home = os.getenv("MESHCORE_HOME_CHANNEL", "").strip()
    if home:
        # Support both dm:pubkey and numeric channel index
        if home.startswith("dm:"):
            pubkey = home.split(":", 1)[1]
            seed["home_channel"] = {"chat_id": f"dm:{pubkey}", "name": f"MeshCore DM ({pubkey[:8]}...)"}
        else:
            seed["home_channel"] = {"chat_id": f"channel:{home}", "name": f"MeshCore Channel {home}"}
    return seed

def interactive_setup():
    from hermes_cli.setup import (prompt, prompt_yes_no, save_env_value, get_env_value,
                                   print_header, print_info, print_warning, print_success)
    print_header("MeshCore")
    existing = get_env_value("MESHCORE_HOST")
    if existing:
        print_info(f"Already configured: {existing}")
        if not prompt_yes_no("Reconfigure?", False):
            return
    host = prompt("MeshCore node hostname", default=existing or "")
    if not host:
        return
    save_env_value("MESHCORE_HOST", host.strip())
    port = prompt("TCP port", default=get_env_value("MESHCORE_PORT") or "5000")
    if port:
        try:
            save_env_value("MESHCORE_PORT", str(int(port)))
        except ValueError:
            pass
    bot_name = prompt("Bot name", default=get_env_value("MESHCORE_BOT_NAME") or "meshcore-bot")
    if bot_name:
        save_env_value("MESHCORE_BOT_NAME", bot_name.strip())
    admin = prompt("Admin pubkey prefixes", default=get_env_value("MESHCORE_ADMIN_NODES") or "")
    if admin:
        save_env_value("MESHCORE_ADMIN_NODES", admin.replace(" ", ""))
    channels = prompt("Monitor channel indexes", default=get_env_value("MESHCORE_MONITOR_CHANNELS") or "")
    if channels:
        save_env_value("MESHCORE_MONITOR_CHANNELS", channels.replace(" ", ""))
    save_env_value("MESHCORE_REQUIRE_MENTION", "true" if prompt_yes_no("Require @mention?", True) else "false")
    save_env_value("MESHCORE_ENABLE_DMS", "true" if prompt_yes_no("Enable DMs?", True) else "false")
    print_success("Saved to ~/.hermes/.env")

def register(ctx):
    ctx.register_platform(
        name="meshcore", label="MeshCore",
        adapter_factory=lambda cfg: MeshCoreAdapter(cfg),
        check_fn=check_requirements, validate_config=validate_config,
        is_connected=is_connected, required_env=["MESHCORE_HOST"],
        install_hint="pip install meshcore", setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MESHCORE_HOME_CHANNEL",
        allowed_users_env="MESHCORE_ALLOWED_USERS",
        allow_all_env="MESHCORE_ALLOW_ALL_USERS",
        max_message_length=400, emoji="📡", pii_safe=True, allow_update_command=True,
        platform_hint=(
            "MeshCore LoRa mesh: 150 char DMs, 135 char channels, auto-split for longer. "
            "Plain text only. Admin nodes get full access; public users restricted. "
            "Never share credentials or sensitive data in public channels."
        ),
    )

    # Register admin tools
    ctx.register_tool(
        name="meshcore_admin",
        toolset="meshcore",
        schema=MESHCORE_ADMIN_SCHEMA,
        handler=lambda args, **kw: _handle_meshcore_admin(
            node=args.get("node", ""),
            command=args.get("command", ""),
            password=args.get("password", "")),
        is_async=True,
        emoji="🛰️",
    )
    ctx.register_tool(
        name="meshcore_contact",
        toolset="meshcore",
        schema=MESHCORE_CONTACT_SCHEMA,
        handler=lambda args, **kw: _handle_meshcore_contact(
            name=args.get("name", "")),
        is_async=True,
        emoji="📇",
    )
    ctx.register_tool(
        name="meshcore_admin_query",
        toolset="meshcore",
        schema={
            "name": "meshcore_admin_query",
            "description": (
                "Query a remote MeshCore repeater via the gateway's file-based request mechanism. "
                "Works from any session — writes a request file, gateway processes it within 15s, "
                "then polls for the response (up to 60s). Use this instead of meshcore_admin when "
                "the gateway process is separate from your session. "
                "Supported commands: ver, board, clock, stats-core, stats-radio, "
                "stats-packets, neighbors, get owner.info, region list allowed, "
                "region list denied, req_status, req_neighbours, req_telemetry, "
                "req_acl, req_owner, req_clock, req_regions. "
                "Note: text CLI commands (set, reboot, gps, etc.) only work over "
                "serial/UART — not available remotely over the mesh."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node name or pubkey prefix to query (e.g. 'SA-MtCompass-RPT', 'ab60ca209921')"
                    },
                    "command": {
                        "type": "string",
                        "description": "CLI command to send (e.g. 'stats-core', 'ver', 'neighbors', 'all')"
                    },
                    "password": {
                        "type": "string",
                        "description": "Optional admin password for the repeater"
                    },
                },
                "required": ["node", "command"],
            },
        },
        handler=lambda args, **kw: _handle_meshcore_admin_query(
            node=args.get("node", ""),
            command=args.get("command", ""),
            password=args.get("password", "")),
        is_async=True,
        emoji="🛰️",
    )
