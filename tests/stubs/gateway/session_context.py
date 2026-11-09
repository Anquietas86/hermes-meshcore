"""Stand-in for gateway.session_context (per-turn session variables)."""

import os
from contextvars import ContextVar

_VARS = {}


def _var(name):
    if name not in _VARS:
        _VARS[name] = ContextVar(name, default=None)
    return _VARS[name]


def set_session_vars(**values):
    keys = {"platform": "HERMES_SESSION_PLATFORM", "chat_id": "HERMES_SESSION_CHAT_ID",
            "chat_type": "HERMES_SESSION_CHAT_TYPE", "user_id": "HERMES_SESSION_USER_ID"}
    return [_var(keys[k]).set(v) for k, v in values.items()]


def clear_session_vars(tokens=None):
    for var in _VARS.values():
        var.set(None)


def get_session_env(name, default=""):
    value = _var(name).get()
    return value if value is not None else os.getenv(name, default)
