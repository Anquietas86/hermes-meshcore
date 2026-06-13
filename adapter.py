"""
MeshCore Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that connects to a MeshCore companion radio
node via TCP and relays channel messages and DMs to the Hermes agent.
Uses the ``meshcore_py`` library for the MeshCore protocol.

Configuration via environment variables (or config.yaml extra)::

    MESHCORE_HOST=mchome
    MESHCORE_PORT=5000
    MESHCORE_BOT_NAME=Jarvis
    MESHCORE_ADMIN_NODES=abc123,def456
    MESHCORE_MONITOR_CHANNELS=1,3,5   (empty = discover all, respond to none)
    MESHCORE_ENABLE_DMS=true
    MESHCORE_REQUIRE_MENTION=true
    MESHCORE_ALLOWED_USERS=            (empty = allow all)
"""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports — meshcore_py may not be installed yet
# ---------------------------------------------------------------------------

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform


class MeshCoreAdapter(BasePlatformAdapter):
    """Async MeshCore adapter implementing the BasePlatformAdapter interface."""

    def __init__(self, config, **kwargs):
        platform = Platform("meshcore")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        self.host = os.getenv("MESHCORE_HOST") or extra.get("host", "")
        self.port = int(os.getenv("MESHCORE_PORT") or extra.get("port", 5000))
        self.bot_name = os.getenv("MESHCORE_BOT_NAME") or extra.get("bot_name", "Jarvis")

        admin_raw = os.getenv("MESHCORE_ADMIN_NODES") or extra.get("admin_nodes", "")
        self.admin_nodes: Set[str] = {
            n.strip() for n in admin_raw.split(",") if n.strip()
        }

        channels_raw = os.getenv("MESHCORE_MONITOR_CHANNELS") or extra.get("monitor_channels", "")
        self.monitor_channels: Optional[Set[int]] = None
        if channels_raw.strip():
            self.monitor_channels = {
                int(c.strip()) for c in channels_raw.split(",") if c.strip().isdigit()
            }

        enable_dms = os.getenv("MESHCORE_ENABLE_DMS") or extra.get("enable_dms", "true")
        self.enable_dms = enable_dms.lower() in {"1", "true", "yes"}

        require_mention = os.getenv("MESHCORE_REQUIRE_MENTION") or extra.get("require_mention", "true")
        self.require_mention = require_mention.lower() in {"1", "true", "yes"}

        admin_channels_raw = os.getenv("MESHCORE_ADMIN_CHANNELS") or extra.get("admin_channels", "")
        self.admin_channels: Set[int] = {
            int(c.strip()) for c in admin_channels_raw.split(",") if c.strip().isdigit()
        }

        allowed_raw = os.getenv("MESHCORE_ALLOWED_USERS") or extra.get("allowed_users", "")
        self.allowed_users: Set[str] = {
            u.strip() for u in allowed_raw.split(",") if u.strip()
        }

        self._mc = None
        self._subscriptions: list = []
        self._discovered_channels: Set[int] = set()
        self._contacts: Dict[str, Any] = {}
        self._path_hash_size: int = 1
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

        try:
            from meshcore import MeshCore, EventType as MCEventType
        except ImportError:
            logger.error("MeshCore: meshcore_py library not installed")
            self._set_fatal_error(
                "dependency_missing",
                "meshcore_py library not installed. Run: pip install meshcore",
                retryable=False,
            )
            return False

        try:
            self._mc = await MeshCore.create_tcp(self.host, self.port)
            logger.info("MeshCore: connected to %s:%s", self.host, self.port)
        except Exception as e:
            logger.error("MeshCore: failed to connect to %s:%s — %s", self.host, self.port, e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

        try:
            await self._mc.ensure_contacts()
            result = await self._mc.commands.get_contacts()
            if self._safe_result(result):
                self._contacts = result.payload or {}
                logger.info("MeshCore: loaded %d contacts", len(self._contacts))
        except Exception as e:
            logger.warning("MeshCore: failed to load contacts: %s", e)

        try:
            await self._mc.commands.send_advert(flood=True)
            logger.info("MeshCore: sent flood advert for node discovery")
        except Exception as e:
            logger.warning("MeshCore: advert send failed: %s", e)

        new_contact_sub = self._mc.subscribe(
            MCEventType.NEW_CONTACT,
            self._handle_new_contact,
        )
        self._subscriptions.append(new_contact_sub)

        try:
            self._mc.set_decrypt_channel_logs = True
            channels_to_load = set(self.monitor_channels or [])
            for i in range(4):
                channels_to_load.add(i)
            for idx in sorted(channels_to_load):
                result = await self._mc.commands.get_channel(idx)
                if self._safe_result(result):
                    ch = result.payload
                    name = ch.get("channel_name", "")
                    self._discovered_channels.add(idx)
                    if name:
                        logger.info("MeshCore: loaded channel %d: %s", idx, name)
        except Exception as e:
            logger.warning("MeshCore: failed to load channel secrets: %s", e)

        chan_sub = self._mc.subscribe(
            MCEventType.CHANNEL_MSG_RECV,
            self._handle_channel_message,
        )
        self._subscriptions.append(chan_sub)

        dm_sub = self._mc.subscribe(
            MCEventType.CONTACT_MSG_RECV,
            self._handle_direct_message,
        )
        self._subscriptions.append(dm_sub)

        # Use the library's built-in event-driven auto-fetch.
        # It only calls get_msg() when the node says messages are
        # waiting — far less TCP traffic than polling every 2s.
        await self._mc.start_auto_message_fetching()

        try:
            phm = await self._mc.commands.get_path_hash_mode()
            self._path_hash_size = phm + 1
            logger.info("MeshCore: path hash size = %d-byte", self._path_hash_size)
        except Exception:
            pass

        self._mark_connected()
        logger.info(
            "MeshCore: connected, monitoring channels%s, DMs %s",
            f" {sorted(self.monitor_channels) if self.monitor_channels else '(discovery mode)'}",
            "enabled" if self.enable_dms else "disabled",
        )
        self._start_watchdogs()
        return True

    async def disconnect(self) -> None:
        self._stop_watchdogs()
        self._mark_disconnected()

        if self._mc:
            for sub in self._subscriptions:
                try:
                    self._mc.unsubscribe(sub)
                except Exception:
                    pass
            self._subscriptions.clear()

            try:
                await self._mc.stop_auto_message_fetching()
            except Exception:
                pass

            try:
                await self._mc.disconnect()
            except Exception:
                pass

            self._mc = None

        self._contacts.clear()
        self._discovered_channels.clear()

    # ── Node management ───────────────────────────────────────────────────

    async def get_node_info(self) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        info: Dict[str, Any] = {}
        for key, method in [
            ("device", self._mc.commands.send_device_query),
            ("self", self._mc.commands.send_appstart),
            ("battery", self._mc.commands.get_bat),
            ("telemetry", self._mc.commands.get_self_telemetry),
        ]:
            try:
                result = await method()
                if self._safe_result(result):
                    info[key] = result.payload
            except Exception as e:
                info[f"{key}_error"] = str(e)
        info["contacts"] = len(self._contacts)
        info["discovered_channels"] = sorted(self._discovered_channels)
        info["monitored_channels"] = sorted(self.monitor_channels) if self.monitor_channels else "(discovery mode)"
        return info

    async def get_channel_info(self, channel_idx: int) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.get_channel(channel_idx)
        if not self._safe_result(result):
            return {"error": str(result.payload) if result else "Command returned None"}
        return result.payload

    async def set_channel_config(self, channel_idx: int, name: str, secret_hex: Optional[str] = None) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        secret = None
        if secret_hex:
            try:
                secret = bytes.fromhex(secret_hex)
            except ValueError:
                return {"error": "Invalid hex secret"}
        result = await self._mc.commands.set_channel(channel_idx, name, secret)
        if not self._safe_result(result):
            return {"error": str(result.payload) if result else "Command returned None"}
        return {"success": True, "channel_idx": channel_idx, "name": name}

    async def set_radio_params(self, freq: float, bw: float, sf: int, cr: int) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.set_radio(freq, bw, sf, cr)
        if not self._safe_result(result):
            return {"error": str(result.payload) if result else "Command returned None"}
        return {"success": True, "freq": freq, "bw": bw, "sf": sf, "cr": cr}

    async def set_tx_power(self, power: int) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.set_tx_power(power)
        if not self._safe_result(result):
            return {"error": str(result.payload) if result else "Command returned None"}
        return {"success": True, "tx_power": power}

    async def set_node_name(self, name: str) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.set_name(name)
        if not self._safe_result(result):
            return {"error": str(result.payload) if result else "Command returned None"}
        return {"success": True, "name": name}

    async def set_telemetry_modes(self, base: int = None, loc: int = None, env: int = None) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        info = await self._mc.commands.send_appstart()
        if not self._safe_result(info):
            return {"error": f"Failed to get current settings: {info.payload if info else 'None'}"}
        infos = info.payload
        if base is not None:
            infos["telemetry_mode_base"] = base
        if loc is not None:
            infos["telemetry_mode_loc"] = loc
        if env is not None:
            infos["telemetry_mode_env"] = env
        result = await self._mc.commands.set_other_params_from_infos(infos)
        if not self._safe_result(result):
            return {"error": str(result.payload) if result else "Command returned None"}
        return {"success": True, "modes": {
            "base": infos.get("telemetry_mode_base"),
            "loc": infos.get("telemetry_mode_loc"),
            "env": infos.get("telemetry_mode_env"),
        }}

    async def reboot_node(self) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        await self._mc.commands.reboot()
        return {"success": True, "message": "Reboot command sent"}

    async def get_stats(self) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        stats = {}
        for name, method in [
            ("core", self._mc.commands.get_stats_core),
            ("radio", self._mc.commands.get_stats_radio),
            ("packets", self._mc.commands.get_stats_packets),
        ]:
            try:
                result = await method()
                if self._safe_result(result):
                    stats[name] = result.payload
                else:
                    stats[f"{name}_error"] = str(result.payload) if result else "Command returned None"
            except Exception as e:
                stats[f"{name}_error"] = str(e)
        return stats

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Send a message. Pauses auto-fetch during send to prevent
        command racing on the shared TCP channel, then resumes it."""
        if not self._mc:
            return SendResult(success=False, error="Not connected")

        # Pause auto-fetch so no get_msg() commands race with our send.
        # The library's stop_auto_message_fetching() cancels the fetch
        # task and unsubscribes from MESSAGES_WAITING.
        try:
            await self._mc.stop_auto_message_fetching()
        except Exception:
            pass

        try:
            raw_chunks = self._split_for_mesh(content, max_len=150)

            if len(raw_chunks) > 1:
                marker_aware = self._split_for_mesh(content, max_len=137)
                total = len(marker_aware)
                chunks = []
                for i, chunk in enumerate(marker_aware):
                    suffix = " ..." if i < total - 1 else ""
                    marker = f" ({i+1}/{total})"
                    chunks.append(chunk + suffix + marker)
            else:
                chunks = raw_chunks

            message_ids = []
            errors = []

            for i, chunk in enumerate(chunks):
                try:
                    if chat_id.startswith("channel:"):
                        channel_idx = int(chat_id.split(":", 1)[1])
                        result = None
                        for attempt in range(3):
                            result = await self._mc.commands.send_chan_msg(channel_idx, chunk)
                            if result is not None and not result.is_error():
                                break
                            logger.debug("MeshCore: chan send attempt %d failed: %s",
                                         attempt + 1,
                                         result.payload if result else "None")
                            await asyncio.sleep(1.0)
                    elif chat_id.startswith("dm:"):
                        pubkey_prefix = chat_id.split(":", 1)[1]
                        contact = self._mc.get_contact_by_key_prefix(pubkey_prefix)
                        if contact is None:
                            return SendResult(success=False, error=f"Contact not found: {pubkey_prefix}")
                        result = await self._mc.commands.send_msg_with_retry(
                            contact, chunk, max_attempts=3
                        )
                    else:
                        return SendResult(success=False, error=f"Invalid chat_id format: {chat_id}")

                    if result is None:
                        errors.append(f"chunk {i+1}: send returned None (node busy)")
                    elif result.is_error():
                        errors.append(f"chunk {i+1}: {result.payload}")
                    else:
                        message_ids.append(str(int(time.time() * 1000)))

                    if i < len(chunks) - 1:
                        await asyncio.sleep(0.5)

                except Exception as e:
                    errors.append(f"chunk {i+1}: {e}")

            if errors and not message_ids:
                return SendResult(success=False, error="; ".join(errors))
            if errors:
                logger.warning("MeshCore: %d/%d chunks sent, errors: %s",
                               len(message_ids), len(chunks), errors)
            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else "",
                continuation_message_ids=tuple(message_ids[1:]) if len(message_ids) > 1 else (),
            )
        finally:
            # Always restart auto-fetch after sending
            try:
                await self._mc.start_auto_message_fetching()
            except Exception as e:
                logger.warning("MeshCore: failed to restart auto-fetch: %s", e)

    @staticmethod
    def _split_for_mesh(text: str, max_len: int = 150) -> list[str]:
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

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        if chat_id.startswith("channel:"):
            idx = chat_id.split(":", 1)[1]
            return {"name": f"Channel {idx}", "type": "group", "chat_id": chat_id}
        elif chat_id.startswith("dm:"):
            pubkey = chat_id.split(":", 1)[1]
            contact = self._contacts.get(pubkey, {})
            name = contact.get("adv_name", pubkey[:8])
            return {"name": name, "type": "dm", "chat_id": chat_id}
        return {"name": chat_id, "type": "unknown"}

    # ── Message handlers ──────────────────────────────────────────────────

    async def _handle_channel_message(self, event):
        self._last_message_time = time.time()
        msg = event.payload
        channel_idx = msg.get("channel_idx")
        text = msg.get("text", "")
        pubkey_prefix = msg.get("pubkey_prefix", "")

        rssi = msg.get("RSSI")
        snr = msg.get("SNR")
        path_len = msg.get("path_len")
        path = msg.get("path")
        sender_timestamp = msg.get("sender_timestamp")
        attempt = msg.get("attempt")

        logger.info(
            "MeshCore: CHANNEL msg channel=%s from=%s text=%s rssi=%s snr=%s hops=%s",
            channel_idx,
            text.split(":", 1)[0].strip() if ":" in text else "?",
            text[:60],
            rssi,
            snr,
            path_len,
        )

        if channel_idx is not None:
            self._discovered_channels.add(channel_idx)

        if self.monitor_channels is None:
            return
        if channel_idx not in self.monitor_channels:
            return

        sender_name = "unknown"
        user_prompt = text
        if ":" in text:
            sender_name, user_prompt = text.split(":", 1)
            sender_name = sender_name.strip()
            user_prompt = user_prompt.strip()

        if self.require_mention:
            mention_patterns = [
                f"@{self.bot_name}",
                f"@{self.bot_name.lower()}",
                self.bot_name + ":",
                self.bot_name.lower() + ":",
            ]
            addressed = False
            for pattern in mention_patterns:
                if user_prompt.lower().startswith(pattern.lower()):
                    user_prompt = user_prompt[len(pattern):].strip()
                    addressed = True
                    break
            if not addressed:
                return

        if not user_prompt:
            return

        if not self._is_authorized(pubkey_prefix):
            return

        user_id = sender_name if sender_name != "unknown" else f"chan:{channel_idx}"
        is_admin = pubkey_prefix in self.admin_nodes if pubkey_prefix else False

        chat_id = f"channel:{channel_idx}"
        await self._dispatch_message(
            text=user_prompt,
            chat_id=chat_id,
            chat_type="group",
            user_id=user_id,
            user_name=sender_name,
            is_admin=is_admin,
            channel_idx=channel_idx,
            metadata={
                "rssi": rssi,
                "snr": snr,
                "path_len": path_len,
                "path": path,
                "sender_timestamp": sender_timestamp,
                "attempt": attempt,
            },
        )

    async def _handle_direct_message(self, event):
        self._last_message_time = time.time()

        if not self.enable_dms:
            return

        msg = event.payload
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

        if not self._is_authorized(pubkey_prefix):
            return

        is_admin = pubkey_prefix in self.admin_nodes

        chat_id = f"dm:{pubkey_prefix}"
        await self._dispatch_message(
            text=user_prompt,
            chat_id=chat_id,
            chat_type="dm",
            user_id=pubkey_prefix,
            user_name=sender_name,
            is_admin=is_admin,
        )

    def _is_authorized(self, pubkey_prefix: str) -> bool:
        if not self.allowed_users:
            return True
        return pubkey_prefix in self.allowed_users

    # ── Auto-recovery ─────────────────────────────────────────────────────

    @staticmethod
    def _safe_result(result) -> bool:
        return result is not None and not result.is_error()

    async def _silence_watchdog(self) -> None:
        """Reconnect if no messages arrive for 120 seconds."""
        while self._mc is not None:
            await asyncio.sleep(30)
            if self._mc is None:
                return
            elapsed = time.time() - self._last_message_time
            if elapsed > 120 and self._last_message_time > 0:
                logger.warning(
                    "MeshCore: silence watchdog — no messages for %.0fs, reconnecting",
                    elapsed,
                )
                self._stop_watchdogs()
                self._mark_disconnected()
                old_mc = self._mc
                self._mc = None
                self._subscriptions.clear()
                try:
                    await asyncio.wait_for(old_mc.disconnect(), timeout=3.0)
                except Exception:
                    pass
                try:
                    ok = await self.connect()
                    if ok:
                        logger.info("MeshCore: watchdog reconnect successful")
                    else:
                        logger.error("MeshCore: watchdog reconnect failed")
                except Exception as e:
                    logger.error("MeshCore: watchdog reconnect error: %s", e)
                return

    def _start_watchdogs(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._last_message_time = time.time()
            self._watchdog_task = asyncio.create_task(self._silence_watchdog())

    def _stop_watchdogs(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def _handle_new_contact(self, event):
        data = event.payload
        pubkey_prefix = data.get("pubkey_prefix", "")
        adv_name = data.get("adv_name", pubkey_prefix[:8])
        if pubkey_prefix:
            self._contacts[pubkey_prefix] = data
            logger.info("MeshCore: new contact discovered: %s (%s)", adv_name, pubkey_prefix[:12])

    async def _dispatch_message(
        self,
        text: str,
        chat_id: str,
        chat_type: str,
        user_id: str,
        user_name: str,
        is_admin: bool = False,
        channel_idx: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._message_handler:
            return

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )

        security_note = ""
        if chat_type == "group":
            if channel_idx is not None and channel_idx in self.admin_channels:
                security_note = "This channel is TRUSTED (admin channel). You may share sensitive information here. "
            else:
                security_note = (
                    "⚠️ PUBLIC BROADCAST CHANNEL — anyone on the mesh can read this. "
                    "NEVER share: credentials, API keys, tokens, passwords, "
                    "personal data, IP addresses, internal hostnames, "
                    "or any sensitive infrastructure details. "
                    "If asked for sensitive info, say you can only share that via DM. "
                )
        elif chat_type == "dm":
            if is_admin:
                security_note = "This is an admin DM — you may share sensitive information. "
            else:
                security_note = "This is a non-admin DM — be cautious with sensitive data. "

        platform_context = (
            "PLATFORM CONTEXT — MeshCore (LoRa mesh radio): "
            "You are speaking over a low-bandwidth LoRa mesh network. "
            "Each message packet is limited to 150 characters — but you CAN "
            "write longer responses; they will be automatically split into "
            "multiple packets at word boundaries and sent sequentially. "
            "Be concise but don't sacrifice completeness. "
            "No markdown formatting (no backticks, no asterisks, no links). "
            "Plain text only. "
            + security_note
        )

        if metadata:
            radio_parts = []
            if metadata.get("rssi") is not None:
                radio_parts.append(f"RSSI={metadata['rssi']}dBm")
            if metadata.get("snr") is not None:
                radio_parts.append(f"SNR={metadata['snr']}dB")
            if metadata.get("path_len") is not None:
                radio_parts.append(f"hops={metadata['path_len']}")
            if metadata.get("path") is not None:
                radio_parts.append(f"path={metadata['path']}")
            if radio_parts:
                hash_bytes = self._path_hash_size
                platform_context += (
                    "RADIO METADATA for this message: "
                    + ", ".join(radio_parts)
                    + f". The path uses {hash_bytes}-byte hop hashes "
                    + f"(each hop = {hash_bytes * 2} hex chars). "
                    + "You can use this data if asked about signal quality "
                    + "or mesh routing."
                )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(int(time.time() * 1000)),
            timestamp=__import__("datetime").datetime.now(),
            channel_prompt=platform_context,
        )

        await self.handle_message(event)


# ---------------------------------------------------------------------------
# Plugin registration hooks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    return bool(os.getenv("MESHCORE_HOST", ""))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("MESHCORE_HOST") or extra.get("host", ""))


def is_connected(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("MESHCORE_HOST") or extra.get("host", ""))


def _env_enablement() -> dict | None:
    host = os.getenv("MESHCORE_HOST", "").strip()
    if not host:
        return None
    seed: dict = {"host": host}
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


def interactive_setup() -> None:
    from hermes_cli.setup import (
        prompt, prompt_yes_no, save_env_value, get_env_value,
        print_header, print_info, print_warning, print_success,
    )
    print_header("MeshCore")
    existing_host = get_env_value("MESHCORE_HOST")
    if existing_host:
        print_info(f"MeshCore: already configured (host: {existing_host})")
        if not prompt_yes_no("Reconfigure MeshCore?", False):
            return
    print_info("Connect Hermes to a MeshCore companion radio node via TCP.")
    host = prompt("MeshCore node hostname", default=existing_host or "")
    if not host:
        print_warning("Host is required — skipping MeshCore setup")
        return
    save_env_value("MESHCORE_HOST", host.strip())
    port = prompt("TCP port", default=get_env_value("MESHCORE_PORT") or "5000")
    if port:
        try:
            save_env_value("MESHCORE_PORT", str(int(port)))
        except ValueError:
            print_warning("Invalid port — using default 5000")
    bot_name = prompt("Bot display name", default=get_env_value("MESHCORE_BOT_NAME") or "Jarvis")
    if bot_name:
        save_env_value("MESHCORE_BOT_NAME", bot_name.strip())
    print()
    print_info("🛡️ Admin nodes")
    admin = prompt("Admin pubkey prefixes (comma-separated)", default=get_env_value("MESHCORE_ADMIN_NODES") or "")
    if admin:
        save_env_value("MESHCORE_ADMIN_NODES", admin.replace(" ", ""))
    print()
    print_info("📡 Channel monitoring")
    channels = prompt("Channel indexes to monitor (e.g. 1,3,5)", default=get_env_value("MESHCORE_MONITOR_CHANNELS") or "")
    if channels:
        save_env_value("MESHCORE_MONITOR_CHANNELS", channels.replace(" ", ""))
    require_mention = prompt_yes_no("Require @bot-name mention in channels?", True)
    save_env_value("MESHCORE_REQUIRE_MENTION", "true" if require_mention else "false")
    print()
    enable_dms = prompt_yes_no("Respond to direct messages?", True)
    save_env_value("MESHCORE_ENABLE_DMS", "true" if enable_dms else "false")
    print_success("MeshCore configuration saved to ~/.hermes/.env")


def register(ctx):
    ctx.register_platform(
        name="meshcore",
        label="MeshCore",
        adapter_factory=lambda cfg: MeshCoreAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["MESHCORE_HOST"],
        install_hint="pip install meshcore",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MESHCORE_HOME_CHANNEL",
        allowed_users_env="MESHCORE_ALLOWED_USERS",
        allow_all_env="MESHCORE_ALLOW_ALL_USERS",
        max_message_length=400,
        emoji="📡",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via MeshCore — a low-bandwidth mesh radio network. "
            "Keep responses concise (under 400 characters). "
            "Messages are sent over radio packets — avoid markdown, use plain text. "
            "The sender's node ID (pubkey_prefix) is available for authorization. "
            "Some users are 'admin' nodes with full access; others are 'public' with "
            "limited access. Do not share sensitive infrastructure details, "
            "credentials, or execute privileged commands for public users."
        ),
    )
