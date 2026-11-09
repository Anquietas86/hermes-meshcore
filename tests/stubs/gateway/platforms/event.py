from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class MessageType(Enum):
    TEXT = "text"


@dataclass
class MessageEvent:
    text: str
    message_type: MessageType = MessageType.TEXT
    source: Any = None
    message_id: Optional[str] = None
    channel_prompt: Optional[str] = None
    channel_context: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    allow_gateway_control: bool = True
