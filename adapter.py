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
    MESHCORE_BOT_NAME=Jarvis
    MESHCORE_ADMIN_NODES=bba647077b2c
    MESHCORE_MONITOR_CHANNELS=1
    MESHCORE_ENABLE_DMS=true
"""

import asyncio
import io
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
            # Find frame marker
            while True:
                b = await self._read_exactly(1)
                if b[0] == FRAME_RECV_MARKER:
                    break
                logger.debug("MeshCore(raw): skipping junk byte 0x%02x", b[0])

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
        """Parse CONTACT_MSG_RECV (0x07) or V3 (0x10)."""
        buf = io.BytesIO(payload)
        msg = {"type": "PRIV"}
        if is_v3:
            msg["SNR"] = int.from_bytes(buf.read(1), "little", signed=True) / 4
            buf.read(2)  # reserved
        msg["pubkey_prefix"] = buf.read(6).hex()
        plen = buf.read(1)[0]
        if plen == 255:
            msg["path_hash_mode"] = -1
            msg["path_len"] = 255
        else:
            msg["path_hash_mode"] = plen >> 6
            msg["path_len"] = plen & 0x3F
        msg["txt_type"] = buf.read(1)[0]
        msg["sender_timestamp"] = int.from_bytes(buf.read(4), "little")
        if msg["txt_type"] == 2:
            msg["signature"] = buf.read(4).hex()
        msg["text"] = buf.read().decode("utf-8", "ignore")
        return msg

    @staticmethod
    def parse_channel_msg(payload: bytes, is_v3: bool = False) -> dict:
        """Parse CHANNEL_MSG_RECV (0x08) or V3 (0x11)."""
        buf = io.BytesIO(payload)
        msg = {"type": "CHAN"}
        if is_v3:
            msg["SNR"] = int.from_bytes(buf.read(1), "little", signed=True) / 4
            buf.read(2)  # reserved
        msg["channel_idx"] = buf.read(1)[0]
        plen = buf.read(1)[0]
        if plen == 255:
            msg["path_hash_mode"] = -1
            msg["path_len"] = 255
        else:
            msg["path_hash_mode"] = plen >> 6
            msg["path_len"] = plen & 0x3F
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

    def __init__(self, config, **kwargs):
        platform = Platform("meshcore")
        super().__init__(config=config, platform=platform)
        extra = getattr(config, "extra", {}) or {}

        self.host = os.getenv("MESHCORE_HOST") or extra.get("host", "")
        self.port = int(os.getenv("MESHCORE_PORT") or extra.get("port", 5000))
        self.bot_name = os.getenv("MESHCORE_BOT_NAME") or extra.get("bot_name", "Jarvis")  # fallback

        admin_raw = os.getenv("MESHCORE_ADMIN_NODES") or extra.get("admin_nodes", "")
        self.admin_nodes: Set[str] = {n.strip() for n in admin_raw.split(",") if n.strip()}

        channels_raw = os.getenv("MESHCORE_MONITOR_CHANNELS") or extra.get("monitor_channels", "")
        self.monitor_channels: Optional[Set[int]] = None
        if channels_raw.strip():
            self.monitor_channels = {int(c.strip()) for c in channels_raw.split(",") if c.strip().isdigit()}

        enable_dms = os.getenv("MESHCORE_ENABLE_DMS") or extra.get("enable_dms", "true")
        self.enable_dms = enable_dms.lower() in {"1", "true", "yes"}

        require_mention = os.getenv("MESHCORE_REQUIRE_MENTION") or extra.get("require_mention", "true")
        self.require_mention = require_mention.lower() in {"1", "true", "yes"}

        admin_channels_raw = os.getenv("MESHCORE_ADMIN_CHANNELS") or extra.get("admin_channels", "")
        self.admin_channels: Set[int] = {int(c.strip()) for c in admin_channels_raw.split(",") if c.strip().isdigit()}

        allowed_raw = os.getenv("MESHCORE_ALLOWED_USERS") or extra.get("allowed_users", "")
        self.allowed_users: Set[str] = {u.strip() for u in allowed_raw.split(",") if u.strip()}

        self._conn: Optional[MeshCoreRawConnection] = None
        self._contacts: Dict[str, dict] = {}
        self._discovered_channels: Set[int] = set()
        self._path_hash_size: int = 1
        self._self_info: dict = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._last_message_time: float = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return "MeshCore"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
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
        logger.info("MeshCore: connected (raw protocol), channels=%s, DMs=%s",
                    sorted(self.monitor_channels) if self.monitor_channels else "(discovery)",
                    "on" if self.enable_dms else "off")
        self._start_poll()
        self._start_keepalive()
        self._start_watchdog()
        return True

    async def disconnect(self) -> None:
        self._stop_watchdog()
        self._stop_keepalive()
        self._stop_poll()
        self._mark_disconnected()
        if self._conn:
            try:
                await self._conn.disconnect()
            except Exception:
                pass
            self._conn = None
        self._contacts.clear()
        self._discovered_channels.clear()

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
                        logger.info("MeshCore: loaded channel %d: %s", idx, name)
            except Exception as e:
                logger.debug("MeshCore: channel %d load failed: %s", idx, e)

    # ── Unsolicited message routing ────────────────────────────────────────

    async def _route_unsolicited(self, pkt_type: int, payload: bytes) -> None:
        """Route unsolicited messages (push notifications, incoming messages)
        that arrive during command waits."""
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
        elif pkt_type == PKT_NEW_ADVERT:
            c = MeshCoreRawConnection.parse_contact(payload)
            pubkey = c.get("public_key", "")
            if pubkey:
                self._contacts[pubkey] = c
                logger.info("MeshCore: new contact: %s", c.get("adv_name", pubkey[:12]))
        # ACK, MESSAGES_WAITING, ADVERTISEMENT — silently consumed

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
        """get_bat() every 30s — keeps TCP pipe alive."""
        while self._conn and self._conn.is_connected:
            await asyncio.sleep(30)
            if not self._conn or not self._conn.is_connected:
                return
            try:
                await self._conn.send_command(b"\x14", [PKT_BATTERY, PKT_ERROR], timeout=5.0)
            except Exception:
                pass

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
                self._stop_keepalive()
                self._stop_poll()
                self._mark_disconnected()
                try:
                    await self._conn.disconnect()
                except Exception:
                    pass
                self._conn = None
                self._contacts.clear()
                try:
                    await self.connect()
                except Exception as e:
                    logger.error("MeshCore: watchdog reconnect error: %s", e)
                return

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        if not self._conn or not self._conn.is_connected:
            return SendResult(success=False, error="Not connected")

        raw_chunks = self._split_for_mesh(content, max_len=150)
        if len(raw_chunks) > 1:
            marker_aware = self._split_for_mesh(content, max_len=137)
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
        """Send a channel message. Returns True on success, error string on failure."""
        timestamp = int(time.time())
        for attempt in range(3):
            cmd = bytes([CMD_SEND_CHANNEL_TXT_MSG, 0x00, channel_idx]) + \
                  timestamp.to_bytes(4, "little") + text.encode("utf-8")
            logger.debug("MeshCore: _send_channel_msg attempt=%d cmd=%s", attempt, cmd.hex()[:30])
            try:
                pkt_type, payload = await self._conn.send_command(
                    cmd, [PKT_OK, PKT_ERROR], timeout=15.0)
                logger.debug("MeshCore: _send_channel_msg result type=0x%02x payload=%s",
                             pkt_type, payload[:20].hex() if payload else "empty")
                if pkt_type == PKT_OK:
                    return True
            except Exception as e:
                logger.debug("MeshCore: _send_channel_msg exception: %s", e)
            await asyncio.sleep(1.0)
        return "no_event_received"

    async def _send_dm(self, pubkey_prefix: str, text: str):
        """Send a DM. Returns True on MSG_SENT (node accepted), error string on failure.

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

        for attempt in range(3):
            cmd = bytes([CMD_SEND_TXT_MSG, 0x00, attempt]) + \
                  timestamp.to_bytes(4, "little") + dst_bytes + text.encode("utf-8")
            logger.debug("MeshCore: _send_dm attempt=%d cmd=%s", attempt, cmd.hex()[:30])
            try:
                pkt_type, payload = await self._conn.send_command(
                    cmd, [PKT_MSG_SENT, PKT_ERROR], timeout=10.0)
                logger.debug("MeshCore: _send_dm result type=0x%02x payload=%s",
                             pkt_type, payload[:20].hex() if payload else "empty")
                if pkt_type == PKT_MSG_SENT:
                    return True
            except Exception as e:
                logger.debug("MeshCore: _send_dm exception: %s", e)
            await asyncio.sleep(1.0)
        return "no_event_received"

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

        logger.info("MeshCore: CHANNEL ch=%s from=%s text=%s",
                    channel_idx, text.split(":",1)[0].strip() if ":" in text else "?", text[:50])

        if channel_idx is not None:
            self._discovered_channels.add(channel_idx)
        if self.monitor_channels is None or channel_idx not in self.monitor_channels:
            return

        sender_name = "unknown"
        user_prompt = text
        if ":" in text:
            sender_name, user_prompt = text.split(":", 1)
            sender_name = sender_name.strip()
            user_prompt = user_prompt.strip()

        if self.require_mention:
            # MeshCore app sends mentions as @[Full Node Name] (bracketed)
            # Also match plain @Jarvis and Jarvis: prefixes
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

        user_id = sender_name if sender_name != "unknown" else f"chan:{channel_idx}"
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
                security_note = ("⚠️ PUBLIC BROADCAST — never share credentials, keys, "
                                 "IPs, hostnames, or personal data here. ")
        elif chat_type == "dm":
            security_note = "Admin DM. " if is_admin else "Non-admin DM — be cautious. "

        platform_context = (
            "PLATFORM CONTEXT — MeshCore LoRa mesh: 150 char packets, "
            "auto-split for longer responses. Plain text only, no markdown. "
            + security_note
        )

        if metadata:
            parts = []
            for key, label in [("rssi", "RSSI"), ("snr", "SNR"), ("path_len", "hops"), ("path", "path")]:
                if metadata.get(key) is not None:
                    parts.append(f"{label}={metadata[key]}")
            if parts:
                platform_context += "RADIO: " + ", ".join(parts) + ". "

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
    bot_name = prompt("Bot name", default=get_env_value("MESHCORE_BOT_NAME") or "Jarvis")
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
            "MeshCore LoRa mesh: 150 char packets, auto-split for longer. "
            "Plain text only. Admin nodes get full access; public users restricted. "
            "Never share credentials or sensitive data in public channels."
        ),
    )
