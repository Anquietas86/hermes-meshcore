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
    # Enforce the public tool allowlist at call time: Hermes adds MCP servers and other plugins'
    # toolsets to a platform's tool list on its own, so the list alone is not a boundary.
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        try:
            from .access import pre_tool_call
            register_hook("pre_tool_call", pre_tool_call)
        except Exception:
            logger.warning("MeshCore: could not register the tool policy hook", exc_info=True)
    else:
        logger.warning("MeshCore: this Hermes has no plugin hooks; public senders' tools are limited "
                       "by toolset only")
