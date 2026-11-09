"""Public tool policy: toolset lists, the pre_tool_call veto and the tool catalog."""

import sys
import types
import unittest

from _load import adapter as A, registry
import fakenode as fn
from gateway import session_context as sc
from test_adapter import AdapterTestBase

import importlib
access = importlib.import_module("meshcore_platform.access")
ALICE6 = fn.FakeNode.ALICE[:12]
BOB6 = fn.FakeNode.BOB[:12]


class PolicyTests(unittest.TestCase):
    def test_no_mcp_unless_a_server_is_named(self):
        self.assertEqual(access.public_toolset_list(["meshcore", "web"], [], servers=set()),
                         ["meshcore", "web", "no_mcp"])
        self.assertEqual(access.public_toolset_list(["meshcore", "mcp-ha_public"], [], servers=set()),
                         ["meshcore", "mcp-ha_public"])
        self.assertEqual(access.public_toolset_list(["meshcore", "openhop"], [], servers={"openhop"}),
                         ["meshcore", "openhop"])

    def test_admin_tool_never_public(self):
        self.assertNotIn("meshcore_admin", access.public_toolset_list(["meshcore_admin", "web"], [], set()))
        self.assertNotIn("meshcore_admin", access.allowed_public_tools(["meshcore"], ["meshcore_admin"]))

    def test_own_tools_allowed_even_if_hermes_does_not_list_them(self):
        import toolsets
        saved = toolsets.TOOLSETS.pop("meshcore")
        try:
            self.assertTrue({"meshcore_contact", "meshcore_nearby"} <= access.allowed_public_tools(["meshcore"], []))
        finally:
            toolsets.TOOLSETS["meshcore"] = saved

    def test_chosen_mcp_tools_bring_only_their_server(self):
        core = types.SimpleNamespace(_mcp_tool_server_names={"mcp__homeassistant__ha_get_state": "homeassistant",
                                                             "mcp_openhop_reboot": "openhop"})
        tools_pkg = types.ModuleType("tools")
        tools_pkg.mcp_tool = core
        saved = {k: sys.modules.get(k) for k in ("tools", "tools.mcp_tool")}
        sys.modules.update({"tools": tools_pkg, "tools.mcp_tool": core})
        try:
            names = access.public_toolset_list(["meshcore", "web"], ["mcp__homeassistant__ha_get_state"])
        finally:
            for k, v in saved.items():
                sys.modules.pop(k, None) if v is None else sys.modules.__setitem__(k, v)
        self.assertEqual(names, ["meshcore", "web", access.PUBLIC_TOOLSET, "mcp-homeassistant"])

    def test_chosen_tools_become_a_toolset(self):
        names = access.public_toolset_list(["meshcore"], ["mcp_openhop_status"], servers=set())
        self.assertIn(access.PUBLIC_TOOLSET, names)
        import toolsets
        self.assertEqual(toolsets.TOOLSETS[access.PUBLIC_TOOLSET]["tools"], ["mcp_openhop_status"])
        self.assertEqual(access.allowed_public_tools(["meshcore", "web"], ["mcp_openhop_status"]),
                         {"meshcore_contact", "meshcore_nearby", "web_search", "web_extract", "mcp_openhop_status"})


class HookTests(AdapterTestBase):
    env = {"MESHCORE_ADMIN_NODES": fn.FakeNode.ALICE[:16], "MESHCORE_ADMIN_CHANNELS": "1",
           "MESHCORE_PUBLIC_TOOLS": "mcp_openhop_status"}

    def call(self, tool, **session):
        tokens = sc.set_session_vars(**session)
        try:
            return access.pre_tool_call(tool_name=tool, args={})
        finally:
            sc.clear_session_vars(tokens)

    async def test_public_sender_is_limited(self):
        registry.register(self.adapter, self.adapter._home_key)
        dm_bob = dict(platform="meshcore", chat_id=f"dm:{BOB6}", chat_type="dm", user_id=BOB6)
        self.assertIsNone(self.call("web_search", **dm_bob))
        self.assertIsNone(self.call("mcp_openhop_status", **dm_bob))
        for tool in ("terminal", "mcp_ha_HassTurnOn", "meshcore_admin", "memory"):
            self.assertEqual(self.call(tool, **dm_bob)["action"], "block", tool)
        public_channel = dict(platform="meshcore", chat_id="channel:4", chat_type="group", user_id="channel:4:Bob")
        self.assertEqual(self.call("terminal", **public_channel)["action"], "block")

    async def test_admins_and_other_platforms_untouched(self):
        registry.register(self.adapter, self.adapter._home_key)
        self.assertIsNone(self.call("terminal", platform="meshcore", chat_id=f"dm:{ALICE6}",
                                    chat_type="dm", user_id=ALICE6))
        self.assertIsNone(self.call("terminal", platform="meshcore", chat_id="channel:1",
                                    chat_type="ChatType.GROUP", user_id="channel:1:Bob"))
        self.assertIsNone(self.call("terminal", platform="telegram", chat_id="1", chat_type="dm", user_id="1"))
        self.assertIsNone(self.call("terminal"))  # no session (CLI)

    async def test_fails_closed_without_adapter(self):
        registry._LIVE.clear()
        self.assertEqual(self.call("web_search", platform="meshcore", chat_id=f"dm:{ALICE6}",
                                   chat_type="dm", user_id=ALICE6)["action"], "block")


class CatalogTests(unittest.TestCase):
    def setUp(self):
        entry = lambda name, toolset, desc="": types.SimpleNamespace(name=name, toolset=toolset, description=desc)
        reg = types.SimpleNamespace(get_all_entries=lambda: [
            entry("web_search", "web", "Search the web"), entry("terminal", "terminal"),
            entry("meshcore_admin", "meshcore_admin"),
            entry("mcp_openhop_status", "mcp-openhop"), entry("mcp_openhop_reboot", "mcp-openhop")])
        core = types.SimpleNamespace(
            _mcp_tool_server_names={"mcp_openhop_status": "openhop", "mcp_openhop_reboot": "openhop"},
            _tool_read_only_hints={"openhop": {"status": True, "reboot": False}})
        self.mods = {
            "tools": types.ModuleType("tools"),
            "tools.registry": types.SimpleNamespace(registry=reg),
            "tools.mcp_tool": core,
            "tools.mcp_tool_schema": types.SimpleNamespace(mcp_prefixed_tool_name=lambda s, t: f"mcp_{s}_{t}"),
            "tools.mcp_tool_scope": types.SimpleNamespace(_resolve_server_key=lambda s: s),
        }
        self.mods["tools"].mcp_tool = core
        self.saved = {k: sys.modules.get(k) for k in self.mods}
        sys.modules.update(self.mods)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_catalog(self):
        rows = {r["name"]: r for r in access.catalog()}
        self.assertNotIn("meshcore_admin", rows)
        self.assertTrue(rows["web_search"]["read_only"])
        self.assertIsNone(rows["terminal"]["read_only"])
        self.assertTrue(rows["mcp_openhop_status"]["read_only"])
        self.assertFalse(rows["mcp_openhop_reboot"]["read_only"])
        self.assertEqual(rows["mcp_openhop_status"]["server"], "openhop")
        self.assertEqual(access.mcp_server_names(), {"openhop"})


if __name__ == "__main__":
    unittest.main()
