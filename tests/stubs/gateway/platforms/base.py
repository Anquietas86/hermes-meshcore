import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from gateway.platforms.event import MessageEvent, MessageType  # noqa: F401


@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False
    continuation_message_ids: tuple = ()


@dataclass
class SessionSource:
    platform: Any
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"
    user_id: Optional[str] = None
    user_name: Optional[str] = None


class BasePlatformAdapter:
    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self._running = False
        self.fatal_error_code = None
        self.fatal_error_message = None
        self.fatal_error_retryable = True
        self.events = []
        self.fatal_notifications = 0
        self.status_history = []

    def _mark_connected(self, **_kw):
        self._running = True
        self.fatal_error_code = None
        self.status_history.append("connected")

    def _mark_disconnected(self):
        self._running = False
        self.status_history.append("disconnected")

    def _set_fatal_error(self, code, message, *, retryable):
        self._running = False
        self.fatal_error_code, self.fatal_error_message = code, message
        self.fatal_error_retryable = retryable
        self.status_history.append(f"fatal:{code}")

    async def _notify_fatal_error(self):
        self.fatal_notifications += 1

    @property
    def has_fatal_error(self):
        return self.fatal_error_code is not None

    def build_source(self, chat_id, chat_name=None, chat_type="dm", user_id=None, user_name=None, **_kw):
        return SessionSource(self.platform, chat_id, chat_name, chat_type, user_id, user_name)

    async def handle_message(self, event):
        self.events.append(event)
