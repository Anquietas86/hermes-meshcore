"""Plugin settings, read from ``config.extra`` and the profile's environment."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from typing import Any, Dict, List, Optional, Set

from . import access
from .textutil import as_bool, as_list, normalize_pubkey, parse_int_set, parse_rate

from gateway.platforms._shared import get_scoped_secret

try:
    from gateway.platforms._shared import extra_or_secret
except ImportError:
    def extra_or_secret(extra, key, env, default="", *, blank_is_unset=True):  # type: ignore[no-redef]
        value = get_scoped_secret(env, None) if env else None
        if value is not None and str(value).strip():
            return value
        value = (extra or {}).get(key)
        if value is None or (blank_is_unset and isinstance(value, str) and not value.strip()):
            return default
        return value

logger = logging.getLogger(__name__)

# Read-only lookups for strangers: the plugin's own tools plus web search/extract.
# Anything that writes (files, terminal, memory, cron, skills management) stays admin-only.
DEFAULT_PUBLIC_TOOLSETS = ("meshcore", "web")

# A DM only carries the first 6 bytes (12 hex chars) of the sender's key, so a shorter
# prefix is weaker than what the radio already gives us: a 32-bit prefix can be matched
# by a generated key in minutes. Keys and prefixes below this length are ignored.
MIN_KEY_HEX = 12
MAX_CHUNKS_CAP = 20


def _keys(raw: Any, env: str) -> Set[str]:
    """Normalised pubkeys/prefixes of at least ``MIN_KEY_HEX`` hex chars; warns about the rest."""
    keys, bad = set(), []
    for item in as_list(raw):
        key = normalize_pubkey(item)
        if key and MIN_KEY_HEX <= len(key) <= 64:
            keys.add(key)
        else:
            bad.append(item)
    if bad:
        logger.warning("MeshCore: ignoring %s entries that are not %d-64 hex chars: %s", env, MIN_KEY_HEX, bad)
    return keys

# ── Settings ─────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Settings:
    host: str = ""
    port: int = 5000
    bot_name: str = ""
    admin_nodes: Set[str] = dataclasses.field(default_factory=set)
    admin_channels: Set[int] = dataclasses.field(default_factory=set)
    monitor_channels: Set[int] = dataclasses.field(default_factory=set)
    require_mention: Optional[Set[int]] = dataclasses.field(default_factory=set)  # None = every channel
    enable_dms: bool = True
    allowed_users: Set[str] = dataclasses.field(default_factory=set)
    allow_all_users: bool = False
    group_allowed_chats: Set[str] = dataclasses.field(default_factory=set)
    max_bytes: int = 130
    max_chunks: int = 6
    advert_on_start: bool = True
    public_toolsets: List[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_PUBLIC_TOOLSETS))
    public_tools: List[str] = dataclasses.field(default_factory=list)
    admin_toolsets: Optional[List[str]] = None
    debug: bool = False
    rate_limit: Optional[tuple] = (6, 600.0)    # (messages, seconds) per sender; None = off
    context_messages: int = 6
    chunk_delay: Optional[float] = None         # None = derive from LoRa airtime
    watch_nodes: List[str] = dataclasses.field(default_factory=list)
    watch_interval: float = 3600.0
    watch_silent_hours: float = 12.0
    watch_battery_mv: int = 3500
    watch_status: bool = True
    watch_notify: str = ""

    @classmethod
    def load(cls, extra: Optional[Dict[str, Any]]) -> "Settings":
        extra = extra or {}

        def get(key: str, env: str, default: Any = "") -> Any:
            return extra_or_secret(extra, key, env, default)

        s = cls()
        s.host = str(get("host", "MESHCORE_HOST", "")).strip()
        try:
            s.port = int(get("port", "MESHCORE_PORT", 5000))
        except (TypeError, ValueError):
            s.port = 5000
        s.bot_name = str(get("bot_name", "MESHCORE_BOT_NAME", "")).strip()
        s.admin_nodes = _keys(get("admin_nodes", "MESHCORE_ADMIN_NODES"), "MESHCORE_ADMIN_NODES")
        s.admin_channels = parse_int_set(get("admin_channels", "MESHCORE_ADMIN_CHANNELS"))
        s.monitor_channels = parse_int_set(get("monitor_channels", "MESHCORE_MONITOR_CHANNELS"))
        mention = get("require_mention", "MESHCORE_REQUIRE_MENTION", "")
        if isinstance(mention, bool):
            s.require_mention = None if mention else set()
        elif str(mention).strip().lower() in {"true", "yes", "on", "all", "1"} and "," not in str(mention):
            # "1" is ambiguous (legacy boolean vs channel 1); earlier releases treated it as "all".
            s.require_mention = None
        else:
            s.require_mention = parse_int_set(mention)
        s.enable_dms = as_bool(get("enable_dms", "MESHCORE_ENABLE_DMS", None), True)
        allowed = as_list(get("allowed_users", "MESHCORE_ALLOWED_USERS"))
        s.allowed_users = _keys([a for a in allowed if not a.lower().startswith("channel:")],
                                "MESHCORE_ALLOWED_USERS")
        s.allow_all_users = as_bool(get("allow_all_users", "MESHCORE_ALLOW_ALL_USERS", None), False)
        chats = {c.lower() for c in as_list(get("group_allowed_chats", "MESHCORE_GROUP_ALLOWED_CHATS"))}
        # Earlier releases documented "channel:3" inside MESHCORE_ALLOWED_USERS.
        legacy = {a.lower() for a in allowed if a.lower().startswith("channel:")}
        if legacy:
            logger.warning("MeshCore: %s in MESHCORE_ALLOWED_USERS is no longer honoured by the gateway's "
                           "allowlist; move it to MESHCORE_GROUP_ALLOWED_CHATS", ",".join(sorted(legacy)))
        chats |= legacy
        s.group_allowed_chats = {f"channel:{c}" if c.isdigit() else c for c in chats}
        try:
            s.max_bytes = max(40, min(200, int(get("max_message_bytes", "MESHCORE_MAX_MESSAGE_BYTES", 130))))
        except (TypeError, ValueError):
            pass
        try:
            s.max_chunks = max(1, min(MAX_CHUNKS_CAP, int(get("max_chunks", "MESHCORE_MAX_CHUNKS", 6))))
        except (TypeError, ValueError):
            pass
        s.advert_on_start = as_bool(get("advert_on_start", "MESHCORE_ADVERT_ON_START", None), True)
        public = as_list(get("public_toolsets", "MESHCORE_PUBLIC_TOOLSETS"))
        s.public_toolsets = public or list(DEFAULT_PUBLIC_TOOLSETS)
        s.public_tools = [t for t in as_list(get("public_tools", "MESHCORE_PUBLIC_TOOLS"))
                          if t not in access.ADMIN_ONLY_TOOLS]
        admin = as_list(get("admin_toolsets", "MESHCORE_ADMIN_TOOLSETS"))
        s.admin_toolsets = admin or None
        s.debug = as_bool(get("debug", "MESHCORE_DEBUG", None), False)
        s.rate_limit = parse_rate(get("rate_limit", "MESHCORE_RATE_LIMIT", ""))
        try:
            s.context_messages = max(0, min(20, int(get("channel_context", "MESHCORE_CHANNEL_CONTEXT", 6))))
        except (TypeError, ValueError):
            pass
        delay = str(get("chunk_delay", "MESHCORE_CHUNK_DELAY", "") or "").strip().lower()
        if delay and delay != "auto":
            with contextlib.suppress(ValueError):
                s.chunk_delay = max(0.0, min(30.0, float(delay)))
        s.watch_nodes = as_list(get("watch_nodes", "MESHCORE_WATCH_NODES"))
        for attr, key, env, conv, lo, hi in (
                ("watch_interval", "watch_interval_minutes", "MESHCORE_WATCH_INTERVAL_MINUTES", float, 5, 1440),
                ("watch_silent_hours", "watch_silent_hours", "MESHCORE_WATCH_SILENT_HOURS", float, 1, 168),
                ("watch_battery_mv", "watch_battery_mv", "MESHCORE_WATCH_BATTERY_MV", int, 0, 5000)):
            raw = get(key, env, None)
            if raw not in (None, ""):
                with contextlib.suppress(TypeError, ValueError):
                    value = max(lo, min(hi, conv(raw)))
                    setattr(s, attr, value * 60 if attr == "watch_interval" else value)
        s.watch_status = as_bool(get("watch_status", "MESHCORE_WATCH_STATUS", None), True)
        s.watch_notify = normalize_chat_id(str(get("watch_notify", "MESHCORE_WATCH_NOTIFY", "")
                                               or get("home_channel", "MESHCORE_HOME_CHANNEL", "") or ""))
        return s

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_nodes or self.admin_channels)


def normalize_chat_id(chat_id: str) -> str:
    """Accept ``channel:N``, ``dm:<hex>``, a bare channel index, or a bare pubkey prefix."""
    raw = str(chat_id or "").strip()
    low = raw.lower()
    if low.startswith("channel:") and low[8:].isdigit():
        return f"channel:{int(low[8:])}"
    if low.startswith("dm:") and normalize_pubkey(low[3:]):
        return f"dm:{low[3:]}"
    if low.isdigit():
        return f"channel:{int(low)}"
    if len(low) >= 8 and normalize_pubkey(low):
        return f"dm:{low}"
    return ""
