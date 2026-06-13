"""
MeshCore Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that connects to a MeshCore companion radio
node via TCP and relays channel messages and DMs to the Hermes agent.
Uses the ``meshcore_py`` library for the MeshCore protocol.

Configuration via environment variables (or config.yaml extra)::

    MESHORE_HOST=mchome
    MESHORE_PORT=5000
    MESHORE_BOT_NAME=Jarvis
    MESHORE_ADMIN_NODES=abc123,def456
    MESHORE_MONITOR_CHANNELS=1,3,5   (empty = discover all, respond to none)
    MESHORE_ENABLE_DMS=true
    MESHORE_REQUIRE_MENTION=true
    MESHORE_ALLOWED_USERS=            (empty = allow all)
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

# meshcore_py is imported at connect time so the plugin is importable
# even before the library is installed.


# ---------------------------------------------------------------------------
# MeshCore Adapter
# ---------------------------------------------------------------------------

class MeshCoreAdapter(BasePlatformAdapter):
    """Async MeshCore adapter implementing the BasePlatformAdapter interface."""

    def __init__(self, config, **kwargs):
        platform = Platform("meshcore")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # Connection settings
        self.host = os.getenv("MESHORE_HOST") or extra.get("host", "")
        self.port = int(os.getenv("MESHORE_PORT") or extra.get("port", 5000))
        self.bot_name = os.getenv("MESHORE_BOT_NAME") or extra.get("bot_name", "Jarvis")

        # Auth — node ID based
        admin_raw = os.getenv("MESHORE_ADMIN_NODES") or extra.get("admin_nodes", "")
        self.admin_nodes: Set[str] = {
            n.strip() for n in admin_raw.split(",") if n.strip()
        }

        # Channel monitoring
        channels_raw = os.getenv("MESHORE_MONITOR_CHANNELS") or extra.get("monitor_channels", "")
        self.monitor_channels: Optional[Set[int]] = None
        if channels_raw.strip():
            self.monitor_channels = {
                int(c.strip()) for c in channels_raw.split(",") if c.strip().isdigit()
            }

        # DM support
        enable_dms = os.getenv("MESHORE_ENABLE_DMS") or extra.get("enable_dms", "true")
        self.enable_dms = enable_dms.lower() in {"1", "true", "yes"}

        # Channel mention requirement
        require_mention = os.getenv("MESHORE_REQUIRE_MENTION") or extra.get("require_mention", "true")
        self.require_mention = require_mention.lower() in {"1", "true", "yes"}

        # Admin channels — channels trusted for admin-level replies
        admin_channels_raw = os.getenv("MESHORE_ADMIN_CHANNELS") or extra.get("admin_channels", "")
        self.admin_channels: Set[int] = {
            int(c.strip()) for c in admin_channels_raw.split(",") if c.strip().isdigit()
        }

        # User allowlist
        allowed_raw = os.getenv("MESHORE_ALLOWED_USERS") or extra.get("allowed_users", "")
        self.allowed_users: Set[str] = {
            u.strip() for u in allowed_raw.split(",") if u.strip()
        }

        # Runtime state
        self._mc = None  # MeshCore client instance
        self._subscriptions: list = []
        self._discovered_channels: Set[int] = set()
        self._contacts: Dict[str, Any] = {}
        self._path_hash_size: int = 1  # Default 1-byte, updated on connect
        self._health_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return "MeshCore"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
        """Connect to the MeshCore node via TCP and subscribe to events."""
        if not self.host:
            logger.error("MeshCore: MESHORE_HOST must be configured")
            self._set_fatal_error(
                "config_missing",
                "MESHORE_HOST must be set",
                retryable=False,
            )
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

        # Load contacts and trigger exchange
        try:
            await self._mc.ensure_contacts()
            result = await self._mc.commands.get_contacts()
            if not result.is_error():
                self._contacts = result.payload or {}
                logger.info("MeshCore: loaded %d contacts", len(self._contacts))
        except Exception as e:
            logger.warning("MeshCore: failed to load contacts: %s", e)

        # Send flood advert so other nodes can discover us
        try:
            await self._mc.commands.send_advert(flood=True)
            logger.info("MeshCore: sent flood advert for node discovery")
        except Exception as e:
            logger.warning("MeshCore: advert send failed: %s", e)

        # Subscribe to new contacts so we pick up nodes as they appear
        new_contact_sub = self._mc.subscribe(
            MCEventType.NEW_CONTACT,
            self._handle_new_contact,
        )
        self._subscriptions.append(new_contact_sub)

        # Load channel decryption secrets — required before auto-fetch
        # so channel messages can be decrypted and delivered.
        # Always load ALL channels (not just monitored) — the node needs
        # every channel secret registered before it delivers any CHANNEL_MSG.
        try:
            self._mc.set_decrypt_channel_logs = True
            channels_to_load = set(self.monitor_channels or [])
            # Also load 0-3 unconditionally — channel 0 (Public) is always
            # active and its secret must be loaded for the auto-fetch loop.
            for i in range(4):
                channels_to_load.add(i)
            for idx in sorted(channels_to_load):
                result = await self._mc.commands.get_channel(idx)
                if not result.is_error():
                    ch = result.payload
                    name = ch.get("channel_name", "")
                    self._discovered_channels.add(idx)
                    if name:
                        logger.info("MeshCore: loaded channel %d: %s", idx, name)
        except Exception as e:
            logger.warning("MeshCore: failed to load channel secrets: %s", e)

        # Subscribe to channel messages
        chan_sub = self._mc.subscribe(
            MCEventType.CHANNEL_MSG_RECV,
            self._handle_channel_message,
        )
        self._subscriptions.append(chan_sub)

        # Subscribe to direct messages
        dm_sub = self._mc.subscribe(
            MCEventType.CONTACT_MSG_RECV,
            self._handle_direct_message,
        )
        self._subscriptions.append(dm_sub)

        # Start auto-fetching messages from the device
        await self._mc.start_auto_message_fetching()

        # Fetch path hash size for correct path interpretation
        try:
            phm = await self._mc.commands.get_path_hash_mode()
            self._path_hash_size = phm + 1  # 0=1-byte, 1=2-byte
            logger.info("MeshCore: path hash size = %d-byte", self._path_hash_size)
        except Exception:
            pass

        self._mark_connected()
        logger.info(
            "MeshCore: connected, monitoring channels%s, DMs %s",
            f" {sorted(self.monitor_channels) if self.monitor_channels else '(discovery mode)'}",
            "enabled" if self.enable_dms else "disabled",
        )

        # Start health check — pings the node every 60s, reconnects if stale
        self._health_task = asyncio.create_task(self._health_check_loop())
        return True

    async def disconnect(self) -> None:
        """Disconnect from the MeshCore node."""
        # Cancel health check loop
        if self._health_task:
            self._health_task.cancel()
            self._health_task = None

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

    async def _health_check_loop(self) -> None:
        """Ping the node every 30s. Single failure triggers disconnect so
        the gateway's auto-reconnect kicks in quickly."""
        while True:
            await asyncio.sleep(30)
            if not self._mc:
                return
            try:
                result = await asyncio.wait_for(
                    self._mc.commands.send_device_query(), timeout=10
                )
                if result.is_error():
                    logger.warning("MeshCore: health ping failed: %s — reconnecting",
                                   result.payload)
                    await self.disconnect()
                    return
                logger.debug("MeshCore: health ping OK (battery=%dmV)",
                            result.payload.get("battery_mv", "?"))
            except asyncio.TimeoutError:
                logger.warning("MeshCore: health ping timed out — reconnecting")
                await self.disconnect()
                return
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("MeshCore: health ping error: %s — reconnecting", e)
                await self.disconnect()
                return

    # ── Node management (admin-only runtime operations) ───────────────────

    async def get_node_info(self) -> Dict[str, Any]:
        """Get device info, battery, and stats from the connected node."""
        if not self._mc:
            return {"error": "Not connected"}

        info: Dict[str, Any] = {}

        try:
            result = await self._mc.commands.send_device_query()
            if not result.is_error():
                info["device"] = result.payload
                # Extract path_hash_mode for convenience
                phm = result.payload.get("path_hash_mode")
                if phm is not None:
                    info["path_hash_mode"] = phm
                    info["path_hash_size"] = phm + 1  # 0=1-byte, 1=2-byte
        except Exception as e:
            info["device_error"] = str(e)

        try:
            result = await self._mc.commands.send_appstart()
            if not result.is_error():
                info["self"] = result.payload
        except Exception as e:
            info["self_error"] = str(e)

        try:
            result = await self._mc.commands.get_bat()
            if not result.is_error():
                info["battery"] = result.payload
        except Exception as e:
            info["battery_error"] = str(e)

        try:
            result = await self._mc.commands.get_self_telemetry()
            if not result.is_error():
                info["telemetry"] = result.payload
        except Exception as e:
            info["telemetry_error"] = str(e)

        info["contacts"] = len(self._contacts)
        info["discovered_channels"] = sorted(self._discovered_channels)
        info["monitored_channels"] = sorted(self.monitor_channels) if self.monitor_channels else "(discovery mode)"

        return info

    async def get_channel_info(self, channel_idx: int) -> Dict[str, Any]:
        """Get info for a specific channel."""
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.get_channel(channel_idx)
        if result.is_error():
            return {"error": str(result.payload)}
        return result.payload

    async def set_channel_config(
        self, channel_idx: int, name: str, secret_hex: Optional[str] = None
    ) -> Dict[str, Any]:
        """Configure a channel name and optional secret.

        If secret_hex is provided, it must be 32 hex chars (16 bytes).
        If omitted, the secret is derived from the channel name hash.
        """
        if not self._mc:
            return {"error": "Not connected"}

        secret = None
        if secret_hex:
            try:
                secret = bytes.fromhex(secret_hex)
            except ValueError:
                return {"error": "Invalid hex secret — must be 32 hex chars (16 bytes)"}

        result = await self._mc.commands.set_channel(channel_idx, name, secret)
        if result.is_error():
            return {"error": str(result.payload)}
        return {"success": True, "channel_idx": channel_idx, "name": name}

    async def set_radio_params(
        self, freq: float, bw: float, sf: int, cr: int
    ) -> Dict[str, Any]:
        """Set radio parameters: frequency (MHz), bandwidth (kHz),
        spreading factor, coding rate."""
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.set_radio(freq, bw, sf, cr)
        if result.is_error():
            return {"error": str(result.payload)}
        return {"success": True, "freq": freq, "bw": bw, "sf": sf, "cr": cr}

    async def set_tx_power(self, power: int) -> Dict[str, Any]:
        """Set TX power level."""
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.set_tx_power(power)
        if result.is_error():
            return {"error": str(result.payload)}
        return {"success": True, "tx_power": power}

    async def set_node_name(self, name: str) -> Dict[str, Any]:
        """Set the MeshCore node's advertised name."""
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.set_name(name)
        if result.is_error():
            return {"error": str(result.payload)}
        return {"success": True, "name": name}

    async def set_telemetry_modes(
        self, base: int = None, loc: int = None, env: int = None
    ) -> Dict[str, Any]:
        """Configure telemetry reporting modes (0-3 each)."""
        if not self._mc:
            return {"error": "Not connected"}

        # Get current settings first
        info = await self._mc.commands.send_appstart()
        if info.is_error():
            return {"error": f"Failed to get current settings: {info.payload}"}

        infos = info.payload
        if base is not None:
            infos["telemetry_mode_base"] = base
        if loc is not None:
            infos["telemetry_mode_loc"] = loc
        if env is not None:
            infos["telemetry_mode_env"] = env

        result = await self._mc.commands.set_other_params_from_infos(infos)
        if result.is_error():
            return {"error": str(result.payload)}
        return {"success": True, "modes": {
            "base": infos.get("telemetry_mode_base"),
            "loc": infos.get("telemetry_mode_loc"),
            "env": infos.get("telemetry_mode_env"),
        }}

    async def reboot_node(self) -> Dict[str, Any]:
        """Reboot the MeshCore node."""
        if not self._mc:
            return {"error": "Not connected"}
        result = await self._mc.commands.reboot()
        return {"success": True, "message": "Reboot command sent"}

    async def get_stats(self) -> Dict[str, Any]:
        """Get node statistics (core, radio, packets)."""
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
                if not result.is_error():
                    stats[name] = result.payload
                else:
                    stats[f"{name}_error"] = str(result.payload)
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
        """Send a message back to a MeshCore channel or contact.

        chat_id format:
          - "channel:<idx>" for channel messages
          - "dm:<pubkey_prefix>" for direct messages

        Messages longer than 150 chars are split into multiple packets
        at word boundaries and sent sequentially with a short delay.

        Opens a fresh TCP connection for each send because the node's
        command channel goes stale under load. The main _mc connection
        stays alive for receiving (auto-fetch loop).
        """
        if not self._mc:
            return SendResult(success=False, error="Not connected")

        # Open a fresh send-only connection
        from meshcore import MeshCore as MC
        send_mc = None
        try:
            send_mc = await MC.create_tcp(self.host, self.port)
        except Exception as e:
            logger.warning("MeshCore: send reconnect failed: %s", e)
            return SendResult(success=False, error=f"Send reconnect failed: {e}")

        try:
            # Split into 150-char chunks at word boundaries
            raw_chunks = self._split_for_mesh(content, max_len=150)

            # Add chunk markers for multi-packet messages
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
                            result = await send_mc.commands.send_chan_msg(channel_idx, chunk)
                            if result is not None and not result.is_error():
                                break
                            logger.debug("MeshCore: chan send attempt %d failed: %s",
                                         attempt + 1,
                                         result.payload if result else "None")
                            await asyncio.sleep(1.0)
                    elif chat_id.startswith("dm:"):
                        pubkey_prefix = chat_id.split(":", 1)[1]
                        contact = send_mc.get_contact_by_key_prefix(pubkey_prefix)
                        if contact is None:
                            return SendResult(success=False, error=f"Contact not found: {pubkey_prefix}")
                        result = await send_mc.commands.send_msg_with_retry(
                            contact, chunk, max_attempts=3
                        )
                    else:
                        return SendResult(success=False, error=f"Invalid chat_id format: {chat_id}")

                    if result is None:
                        errors.append(f"chunk {i+1}: send_chan_msg returned None (node busy)")
                    elif result.is_error():
                        errors.append(f"chunk {i+1}: {result.payload}")
                    else:
                        message_ids.append(str(int(time.time() * 1000)))

                    if i < len(chunks) - 1:
                        await asyncio.sleep(0.5)

                except Exception as e:
                    errors.append(f"chunk {i+1}: {e}")

            if errors and not message_ids:
                logger.warning("MeshCore: all %d chunks failed: %s",
                               len(chunks), "; ".join(errors))
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
            if send_mc:
                try:
                    await send_mc.disconnect()
                except Exception:
                    pass

    @staticmethod
    def _split_for_mesh(text: str, max_len: int = 150) -> list[str]:
        """Split text into chunks ≤ max_len, breaking at word boundaries."""
        chunks = []
        while len(text) > max_len:
            # Find last space within limit
            split_at = text.rfind(" ", 0, max_len)
            if split_at == -1:
                # No space found — hard break
                split_at = max_len
            chunks.append(text[:split_at].strip())
            text = text[split_at:].strip()
        if text:
            chunks.append(text)
        return chunks or [""]

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """MeshCore has no typing indicator — no-op."""
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
        """Handle an incoming channel message from MeshCore."""
        msg = event.payload
        channel_idx = msg.get("channel_idx")
        text = msg.get("text", "")
        pubkey_prefix = msg.get("pubkey_prefix", "")

        # Radio metadata (available when decrypt_channels is enabled)
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

        # Track discovered channels
        if channel_idx is not None:
            self._discovered_channels.add(channel_idx)

        # Check if we should respond to this channel
        if self.monitor_channels is None:
            return  # Discovery mode — don't respond to any channels
        if channel_idx not in self.monitor_channels:
            return  # Not in our monitored set

        # Parse sender name from text (MeshCore format: "sender_name: message")
        sender_name = "unknown"
        user_prompt = text
        if ":" in text:
            sender_name, user_prompt = text.split(":", 1)
            sender_name = sender_name.strip()
            user_prompt = user_prompt.strip()

        # Require @mention in channels (if enabled)
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
                    # Strip the mention prefix
                    user_prompt = user_prompt[len(pattern):].strip()
                    addressed = True
                    break
            if not addressed:
                return  # Not addressed to us

        if not user_prompt:
            return  # Empty message after stripping mention

        # Auth check
        if not self._is_authorized(pubkey_prefix):
            logger.debug("MeshCore: ignoring message from unauthorized node %s", pubkey_prefix[:8])
            return

        # Channel messages are broadcasts — no pubkey_prefix in payload.
        # Use the parsed sender name as user_id so the gateway's auth
        # check doesn't drop the message (user_id=None is rejected).
        user_id = sender_name if sender_name != "unknown" else f"chan:{channel_idx}"

        is_admin = pubkey_prefix in self.admin_nodes if pubkey_prefix else False

        chat_id = f"channel:{channel_idx}"
        logger.info("MeshCore: dispatching channel=%s chat=%s user=%s text=%s", channel_idx, chat_id, user_id, user_prompt[:40])
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
        """Handle an incoming direct message from MeshCore."""
        logger.info("MeshCore: DM received: %s", {k: v for k, v in event.payload.items() if k != 'raw'})

        if not self.enable_dms:
            logger.debug("MeshCore: DMs disabled, ignoring")
            return

        msg = event.payload
        text = msg.get("text", "")
        pubkey_prefix = msg.get("pubkey_prefix", "")

        # Parse sender name
        sender_name = "unknown"
        user_prompt = text
        if ":" in text:
            sender_name, user_prompt = text.split(":", 1)
            sender_name = sender_name.strip()
            user_prompt = user_prompt.strip()

        if not user_prompt:
            return

        # Auth check
        if not self._is_authorized(pubkey_prefix):
            logger.debug("MeshCore: ignoring DM from unauthorized node %s", pubkey_prefix[:8])
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
        """Check if a node is authorized to interact with the bot."""
        if not self.allowed_users:
            return True  # Empty allowlist = allow all
        return pubkey_prefix in self.allowed_users

    async def _handle_new_contact(self, event):
        """Handle a newly discovered contact."""
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
        """Build a MessageEvent and hand it to the base class handler."""
        logger.info("MeshCore: _dispatch_message called chat=%s user=%s text=%s", chat_id, user_id, text[:40])
        if not self._message_handler:
            logger.error("MeshCore: _dispatch_message aborted — no _message_handler set!")
            return

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )

        # Platform context injected into every MeshCore conversation
        # so the model always knows about LoRa/mesh constraints AND security.
        security_note = ""
        if chat_type == "group":
            if channel_idx is not None and channel_idx in self.admin_channels:
                security_note = (
                    "This channel is TRUSTED (admin channel). "
                    "You may share sensitive information here. "
                )
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
                security_note = (
                    "This is an admin DM — you may share sensitive information. "
                )
            else:
                security_note = (
                    "This is a non-admin DM — be cautious with sensitive data. "
                )

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

        # Inject radio metadata into the platform context so the model
        # can reference signal strength, hop count, path, etc. in replies.
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
        logger.info("MeshCore: handle_message returned for chat=%s", chat_id)


# ---------------------------------------------------------------------------
# Plugin registration hooks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Check if MeshCore is configured."""
    host = os.getenv("MESHORE_HOST", "")
    return bool(host)


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    extra = getattr(config, "extra", {}) or {}
    host = os.getenv("MESHORE_HOST") or extra.get("host", "")
    return bool(host)


def is_connected(config) -> bool:
    """Check whether MeshCore is configured (env or config.yaml)."""
    extra = getattr(config, "extra", {}) or {}
    host = os.getenv("MESHORE_HOST") or extra.get("host", "")
    return bool(host)


def _env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from env vars during gateway config load."""
    host = os.getenv("MESHORE_HOST", "").strip()
    if not host:
        return None

    seed: dict = {"host": host}
    port = os.getenv("MESHORE_PORT", "").strip()
    if port:
        try:
            seed["port"] = int(port)
        except ValueError:
            pass

    bot_name = os.getenv("MESHORE_BOT_NAME", "").strip()
    if bot_name:
        seed["bot_name"] = bot_name

    admin_nodes = os.getenv("MESHORE_ADMIN_NODES", "").strip()
    if admin_nodes:
        seed["admin_nodes"] = admin_nodes

    channels = os.getenv("MESHORE_MONITOR_CHANNELS", "").strip()
    if channels:
        seed["monitor_channels"] = channels

    enable_dms = os.getenv("MESHORE_ENABLE_DMS", "").strip()
    if enable_dms:
        seed["enable_dms"] = enable_dms

    require_mention = os.getenv("MESHORE_REQUIRE_MENTION", "").strip()
    if require_mention:
        seed["require_mention"] = require_mention

    allowed = os.getenv("MESHORE_ALLOWED_USERS", "").strip()
    if allowed:
        seed["allowed_users"] = allowed

    # Home channel for cron delivery
    home = os.getenv("MESHORE_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": f"channel:{home}",
            "name": f"MeshCore Channel {home}",
        }

    return seed


def interactive_setup() -> None:
    """Interactive ``hermes gateway setup`` flow for MeshCore."""
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("MeshCore")
    existing_host = get_env_value("MESHORE_HOST")
    if existing_host:
        print_info(f"MeshCore: already configured (host: {existing_host})")
        if not prompt_yes_no("Reconfigure MeshCore?", False):
            return

    print_info("Connect Hermes to a MeshCore companion radio node via TCP.")
    print_info("Requires the meshcore_py library: pip install meshcore")
    print()

    host = prompt("MeshCore node hostname (e.g. mchome)", default=existing_host or "")
    if not host:
        print_warning("Host is required — skipping MeshCore setup")
        return
    save_env_value("MESHORE_HOST", host.strip())

    port = prompt("TCP port", default=get_env_value("MESHORE_PORT") or "5000")
    if port:
        try:
            save_env_value("MESHORE_PORT", str(int(port)))
        except ValueError:
            print_warning(f"Invalid port — using default 5000")

    bot_name = prompt("Bot display name", default=get_env_value("MESHORE_BOT_NAME") or "Jarvis")
    if bot_name:
        save_env_value("MESHORE_BOT_NAME", bot_name.strip())

    print()
    print_info("🔒 Access control")
    print_info("   MeshCore nodes are identified by their public key prefix.")
    print_info("   Leave allowed users empty to allow anyone to talk to the bot.")

    allow_all = prompt_yes_no("Allow all users?", True)
    if allow_all:
        save_env_value("MESHORE_ALLOW_ALL_USERS", "true")
        save_env_value("MESHORE_ALLOWED_USERS", "")
    else:
        save_env_value("MESHORE_ALLOW_ALL_USERS", "false")
        allowed = prompt(
            "Allowed pubkey prefixes (comma-separated)",
            default=get_env_value("MESHORE_ALLOWED_USERS") or "",
        )
        if allowed:
            save_env_value("MESHORE_ALLOWED_USERS", allowed.replace(" ", ""))

    print()
    print_info("🛡️ Admin nodes (full tool access, sensitive info)")
    admin = prompt(
        "Admin pubkey prefixes (comma-separated)",
        default=get_env_value("MESHORE_ADMIN_NODES") or "",
    )
    if admin:
        save_env_value("MESHORE_ADMIN_NODES", admin.replace(" ", ""))
        print_success("Admin nodes configured")
    else:
        print_info("No admin nodes — all users get public-only access")

    print()
    print_info("📡 Channel monitoring")
    print_info("   Leave empty to discover all channels (respond to none until enabled).")
    print_info("   Enter comma-separated channel indexes to monitor specific channels.")
    channels = prompt(
        "Channel indexes to monitor (e.g. 1,3,5)",
        default=get_env_value("MESHORE_MONITOR_CHANNELS") or "",
    )
    if channels:
        save_env_value("MESHORE_MONITOR_CHANNELS", channels.replace(" ", ""))

    require_mention = prompt_yes_no("Require @bot-name mention in channels?", True)
    save_env_value("MESHORE_REQUIRE_MENTION", "true" if require_mention else "false")

    print()
    print_info("💬 Direct messages")
    enable_dms = prompt_yes_no("Respond to direct messages?", True)
    save_env_value("MESHORE_ENABLE_DMS", "true" if enable_dms else "false")

    print()
    print_success("MeshCore configuration saved to ~/.hermes/.env")
    print_info("Restart the gateway for changes to take effect: hermes gateway restart")


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="meshcore",
        label="MeshCore",
        adapter_factory=lambda cfg: MeshCoreAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["MESHORE_HOST"],
        install_hint="pip install meshcore",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MESHORE_HOME_CHANNEL",
        allowed_users_env="MESHORE_ALLOWED_USERS",
        allow_all_env="MESHORE_ALLOW_ALL_USERS",
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
