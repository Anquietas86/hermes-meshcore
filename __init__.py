"""MeshCore platform plugin for Hermes Agent.

``register`` stays import-light: the client tools come from ``tools.py`` and
the adapter (which imports the gateway) loads only when the platform is used.
"""

import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]


def register(ctx) -> None:
    try:
        from .tools import register_tools
        register_tools(ctx)
    except Exception:
        logger.warning("MeshCore: failed to register tools", exc_info=True)
    from .adapter import register_platform
    register_platform(ctx)
