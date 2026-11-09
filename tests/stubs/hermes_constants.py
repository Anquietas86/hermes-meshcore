import contextvars
import os
from pathlib import Path

_override = contextvars.ContextVar("hermes_home_override", default=None)


def set_hermes_home_override(path):
    return _override.set(str(path) if path else None)


def reset_hermes_home_override(token):
    _override.reset(token)


def get_hermes_home():
    value = _override.get()
    if value:
        return Path(value)
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
