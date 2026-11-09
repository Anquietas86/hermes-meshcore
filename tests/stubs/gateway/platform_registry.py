from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class PlatformEntry:
    name: str = ""
    label: str = ""
    adapter_factory: Optional[Callable] = None
    standalone_sender_fn: Optional[Callable] = None
    group_allowed_chats_env: str = ""
    allowlist_suppresses_pairing: bool = False
    display_tier: str = ""
    display_overrides: dict = None
