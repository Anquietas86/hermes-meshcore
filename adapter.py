"""
MeshCore Platform Adapter for Hermes Agent.

Connects to a MeshCore companion radio node via TCP using meshcore_py.
Follows the same pattern as meshcore-cli: auto-fetch always running,
sends happen alongside it. No manual polling, no pause/resume, no locks.

Configuration via environment variables::

    MESHCORE_HOST=mchome
    MESHCORE_PORT=5000
    MESHCORE_BOT_NAME=Jarvis
    MESHCORE_ADMIN_NODES=abc123,def456
    MESHCORE_MONITOR_CHANNELS=1,3,5
    MESHCORE_ENABLE_DMS=true
"""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform


class MeshCoreAdapter(BasePlatformAdapter):

    def __init__(self, config, **kwargs):
        platform = Platform("meshcore")
        super().__init__(config=config, platform=platform)
        extra = getattr(config, "extra", {}) or {}

        self.host = os.getenv("MESHCORE_HOST") or extra.get("host", "")
        self.port = int(os.getenv("MESHCORE_PORT") or extra.get("port", 5000))
        self.bot_name = os.getenv("MESHCORE_BOT_NAME") or extra.get("bot_name", "Jarvis")

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

        self._mc = None
        self._subscriptions: list = []
        self._discovered_channels: Set[int] = set()
        self._contacts: Dict[str, Any] = {}
        self._path_hash_size: int = 1
        self._last_message_time: float = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return "MeshCore"

    async def connect(self) -> bool:
        if not self.host:
            logger.error("MeshCore: MESHCORE_HOST must be configured")
            self._set_fatal_error("config_missing", "MESHCORE_HOST must be set", retryable=False)
            return False

        try:
            from meshcore import MeshCore, EventType as MCEventType
        except ImportError:
            logger.error("MeshCore: meshcore_py library not installed")
            self._set_fatal_error("dependency_missing", "meshcore_py library not installed", retryable=False)
            return False

        try:
            self._mc = await MeshCore.create_tcp(self.host, self.port)
            logger.info("MeshCore: connected to %s:%s", self.host, self.port)
        except Exception as e:
            logger.error("MeshCore: connect failed — %s", e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

        try:
            await self._mc.ensure_contacts()
            result = await self._mc.commands.get_contacts()
            if self._safe_result(result):
                self._contacts = result.payload or {}
                logger.info("MeshCore: loaded %d contacts", len(self._contacts))
        except Exception as e:
            logger.warning("MeshCore: contacts failed: %s", e)

        try:
            await self._mc.commands.send_advert(flood=True)
        except Exception:
            pass

        self._subscriptions.append(self._mc.subscribe(MCEventType.NEW_CONTACT, self._handle_new_contact))

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
            logger.warning("MeshCore: channel secrets failed: %s", e)

        self._subscriptions.append(self._mc.subscribe(MCEventType.CHANNEL_MSG_RECV, self._handle_channel_message))
        self._subscriptions.append(self._mc.subscribe(MCEventType.CONTACT_MSG_RECV, self._handle_direct_message))

        # Auto-fetch always running — same as meshcore-cli
        await self._mc.start_auto_message_fetching()

        try:
            phm = await self._mc.commands.get_path_hash_mode()
            self._path_hash_size = phm + 1
        except Exception:
            pass

        self._mark_connected()
        logger.info("MeshCore: connected, channels=%s, DMs=%s",
                    sorted(self.monitor_channels) if self.monitor_channels else "(discovery)",
                    "on" if self.enable_dms else "off")
        self._start_watchdogs()
        self._start_keepalive()
        return True

    async def disconnect(self) -> None:
        self._stop_keepalive()
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
        return info

    async def get_channel_info(self, channel_idx: int) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.get_channel(channel_idx)
        return result.payload if self._safe_result(result) else {"error": str(result.payload) if result else "None"}

    async def set_channel_config(self, channel_idx: int, name: str, secret_hex: Optional[str] = None) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        secret = bytes.fromhex(secret_hex) if secret_hex else None
        result = await self._mc.commands.set_channel(channel_idx, name, secret)
        return {"success": True} if self._safe_result(result) else {"error": str(result.payload) if result else "None"}

    async def set_radio_params(self, freq: float, bw: float, sf: int, cr: int) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.set_radio(freq, bw, sf, cr)
        return {"success": True} if self._safe_result(result) else {"error": str(result.payload) if result else "None"}

    async def set_tx_power(self, power: int) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.set_tx_power(power)
        return {"success": True} if self._safe_result(result) else {"error": str(result.payload) if result else "None"}

    async def set_node_name(self, name: str) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.set_name(name)
        return {"success": True} if self._safe_result(result) else {"error": str(result.payload) if result else "None"}

    async def set_telemetry_modes(self, base=None, loc=None, env=None) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        info = await self._mc.commands.send_appstart()
        if not self._safe_result(info):
            return {"error": "Failed to get settings"}
        infos = info.payload
        if base is not None:
            infos["telemetry_mode_base"] = base
        if loc is not None:
            infos["telemetry_mode_loc"] = loc
        if env is not None:
            infos["telemetry_mode_env"] = env
        result = await self._mc.commands.set_other_params_from_infos(infos)
        return {"success": True} if self._safe_result(result) else {"error": str(result.payload) if result else "None"}

    async def reboot_node(self) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        await self._mc.commands.reboot()
        return {"success": True}

    async def get_stats(self) -> Dict[str, Any]:
        if not self._mc:
            return {"error": "Not connected"}
        stats = {}
        for name, method in [("core", self._mc.commands.get_stats_core),
                             ("radio", self._mc.commands.get_stats_radio),
                             ("packets", self._mc.commands.get_stats_packets)]:
            try:
                result = await method()
                stats[name] = result.payload if self._safe_result(result) else f"error: {result.payload if result else 'None'}"
            except Exception as e:
                stats[name] = str(e)
        return stats

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        """Send a message. Auto-fetch runs alongside — same as meshcore-cli."""
        if not self._mc:
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
                    result = None
                    for attempt in range(3):
                        result = await self._mc.commands.send_chan_msg(channel_idx, chunk)
                        if result is not None and not result.is_error():
                            break
                        await asyncio.sleep(1.0)
                elif chat_id.startswith("dm:"):
                    pubkey_prefix = chat_id.split(":", 1)[1]
                    contact = self._mc.get_contact_by_key_prefix(pubkey_prefix)
                    if contact is None:
                        return SendResult(success=False, error=f"Contact not found: {pubkey_prefix}")
                    result = await self._mc.commands.send_msg_with_retry(contact, chunk, max_attempts=3)
                else:
                    return SendResult(success=False, error=f"Invalid chat_id: {chat_id}")

                if result is None:
                    errors.append(f"chunk {i+1}: None")
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
        return SendResult(
            success=True,
            message_id=message_ids[0] if message_ids else "",
            continuation_message_ids=tuple(message_ids[1:]) if len(message_ids) > 1 else (),
        )

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

    async def _handle_channel_message(self, event):
        self._last_message_time = time.time()
        msg = event.payload
        channel_idx = msg.get("channel_idx")
        text = msg.get("text", "")
        pubkey_prefix = msg.get("pubkey_prefix", "")

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
            patterns = [f"@{self.bot_name}", f"@{self.bot_name.lower()}",
                        self.bot_name + ":", self.bot_name.lower() + ":"]
            addressed = False
            for p in patterns:
                if user_prompt.lower().startswith(p.lower()):
                    user_prompt = user_prompt[len(p):].strip()
                    addressed = True
                    break
            if not addressed:
                return

        if not user_prompt or not self._is_authorized(pubkey_prefix):
            return

        user_id = sender_name if sender_name != "unknown" else f"chan:{channel_idx}"
        is_admin = pubkey_prefix in self.admin_nodes if pubkey_prefix else False

        await self._dispatch_message(
            text=user_prompt, chat_id=f"channel:{channel_idx}", chat_type="group",
            user_id=user_id, user_name=sender_name, is_admin=is_admin,
            channel_idx=channel_idx,
            metadata={"rssi": msg.get("RSSI"), "snr": msg.get("SNR"),
                      "path_len": msg.get("path_len"), "path": msg.get("path"),
                      "sender_timestamp": msg.get("sender_timestamp"),
                      "attempt": msg.get("attempt")},
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

        if not user_prompt or not self._is_authorized(pubkey_prefix):
            return

        await self._dispatch_message(
            text=user_prompt, chat_id=f"dm:{pubkey_prefix}", chat_type="dm",
            user_id=pubkey_prefix, user_name=sender_name,
            is_admin=pubkey_prefix in self.admin_nodes,
        )

    def _is_authorized(self, pubkey_prefix: str) -> bool:
        return not self.allowed_users or pubkey_prefix in self.allowed_users

    # ── Auto-recovery ─────────────────────────────────────────────────────

    @staticmethod
    def _safe_result(result) -> bool:
        return result is not None and not result.is_error()

    async def _silence_watchdog(self) -> None:
        while self._mc is not None:
            await asyncio.sleep(30)
            if self._mc is None:
                return
            if time.time() - self._last_message_time > 120 and self._last_message_time > 0:
                logger.warning("MeshCore: silence watchdog — reconnecting")
                self._stop_watchdogs()
                self._mark_disconnected()
                old_mc, self._mc = self._mc, None
                self._subscriptions.clear()
                try:
                    await asyncio.wait_for(old_mc.disconnect(), timeout=3.0)
                except Exception:
                    pass
                try:
                    await self.connect()
                except Exception as e:
                    logger.error("MeshCore: watchdog reconnect error: %s", e)
                return

    def _start_watchdogs(self):
        if self._watchdog_task is None or self._watchdog_task.done():
            self._last_message_time = time.time()
            self._watchdog_task = asyncio.create_task(self._silence_watchdog())

    def _stop_watchdogs(self):
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    # ── TCP keepalive ─────────────────────────────────────────────────────

    def _start_keepalive(self):
        """Ping the node every 30s to prevent TCP idle timeout (~60s)."""
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _stop_keepalive(self):
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    async def _keepalive_loop(self):
        """get_bat() every 30s — lightweight, keeps TCP pipe alive."""
        while self._mc is not None:
            await asyncio.sleep(30)
            if self._mc is None:
                return
            try:
                await self._mc.commands.get_bat()
            except Exception:
                pass

    async def _handle_new_contact(self, event):
        data = event.payload
        pubkey_prefix = data.get("pubkey_prefix", "")
        if pubkey_prefix:
            self._contacts[pubkey_prefix] = data

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
