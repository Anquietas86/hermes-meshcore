"""Live adapters in this process, keyed by their profile's Hermes home.

Kept dependency-free so ``tools.py`` can import it in CLI/TUI processes
without pulling in the gateway.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import state

_LIVE: Dict[str, Any] = {}


def register(adapter: Any, home_key: str) -> None:
    _LIVE[home_key] = adapter


def unregister(adapter: Any, home_key: str) -> None:
    if _LIVE.get(home_key) is adapter:
        del _LIVE[home_key]


def current() -> Optional[Any]:
    """The adapter serving the active profile, if it runs in this process.

    Strict: never falls back to another profile's adapter.
    """
    return _LIVE.get(state.home_key())
