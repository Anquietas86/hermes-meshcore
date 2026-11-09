"""Smoke test against a real hermes-agent checkout (run in CI).

Usage: HERMES_SOURCE_ROOT=/path/to/hermes-agent python3 tests/smoke_hermes.py

Registers the platform in Hermes's real registry (the same PlatformEntry
call PluginContext.register_platform makes, so unknown fields fail loudly),
then runs one adapter against the fake node using Hermes's real base class,
config and event types.
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
HERMES = Path(os.environ["HERMES_SOURCE_ROOT"]).resolve()
sys.path.insert(0, str(HERMES))
sys.path.insert(1, str(TESTS))  # fakenode only; never the stubs

HOME = tempfile.mkdtemp(prefix="hermes-home-")
os.environ["HERMES_HOME"] = HOME

import fakenode as fn  # noqa: E402


def load_plugin():
    spec = importlib.util.spec_from_file_location("meshcore_platform", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["meshcore_platform"] = module
    spec.loader.exec_module(module)
    return module


class Ctx:
    """Mirrors PluginContext.register_platform / register_tool closely enough to catch contract drift."""

    def __init__(self):
        self.tools = []

    def register_platform(self, name, label, adapter_factory, check_fn, validate_config=None,
                          required_env=None, install_hint="", **entry_kwargs):
        from gateway.platform_registry import PlatformEntry, platform_registry
        entry_kwargs.setdefault("plugin_name", "meshcore-platform")
        entry = PlatformEntry(name=name, label=label, adapter_factory=adapter_factory, check_fn=check_fn,
                              validate_config=validate_config, required_env=required_env or [],
                              install_hint=install_hint, source="plugin", **entry_kwargs)
        platform_registry.register(entry)

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                      is_async=False, description="", emoji="", override=False):
        assert schema["name"] == name and is_async
        self.tools.append((name, toolset))


async def main() -> None:
    plugin = load_plugin()
    ctx = Ctx()
    plugin.register(ctx)
    assert [t[0] for t in ctx.tools] == ["meshcore_contact", "meshcore_admin"], ctx.tools

    from gateway.config import Platform, PlatformConfig
    from gateway.platform_registry import platform_registry
    entry = platform_registry.get("meshcore")
    assert entry is not None and entry.standalone_sender_fn is not None
    assert entry.group_allowed_chats_env == "MESHCORE_GROUP_ALLOWED_CHATS"
    assert Platform("meshcore").value == "meshcore"

    node = await fn.FakeNode().start()
    os.environ.update(MESHCORE_HOST="127.0.0.1", MESHCORE_PORT=str(node.port), MESHCORE_MONITOR_CHANNELS="1",
                      MESHCORE_ADMIN_NODES=fn.FakeNode.ALICE, MESHCORE_ADVERT_ON_START="false")
    adapter = entry.adapter_factory(PlatformConfig(enabled=True, extra={}))
    events = []

    async def capture(event):
        events.append(event)
    adapter.handle_message = capture

    assert await adapter.connect(), getattr(adapter, "fatal_error_message", "connect failed")
    try:
        await node.deliver(0x10, fn.contact_msg_body(fn.FakeNode.BOB[:12], "hello: mesh"))
        for _ in range(100):
            if events:
                break
            await asyncio.sleep(0.02)
        assert events, "DM was not dispatched"
        event = events[0]
        assert event.text == "hello: mesh", event.text
        assert event.source.chat_id == "dm:" + fn.FakeNode.BOB[:12]
        assert adapter.toolsets_for_source(event.source) == ["meshcore"]
        result = await adapter.send(event.source.chat_id, "hi back")
        assert result.success, result.error
        result = await adapter.query_remote("Hilltop RPT", "status")
        assert result["success"], result
    finally:
        await adapter.disconnect()
        await node.stop()
    print("smoke test passed against", HERMES)


if __name__ == "__main__":
    asyncio.run(main())
