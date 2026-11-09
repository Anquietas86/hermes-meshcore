"""Adapter behaviour against a fake companion node over real TCP."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from _load import adapter as A, protocol as p, registry, state, tools
import fakenode as fn
from gateway import status as gw_status
from gateway.config import PlatformConfig

ALICE6 = fn.FakeNode.ALICE[:12]
BOB6 = fn.FakeNode.BOB[:12]


class AdapterTestBase(unittest.IsolatedAsyncioTestCase):
    env: dict = {}

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.node = await fn.FakeNode().start()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESHCORE_")}
        env.update(HERMES_HOME=self.tmp.name, MESHCORE_HOST="127.0.0.1", MESHCORE_PORT=str(self.node.port),
                   MESHCORE_MONITOR_CHANNELS="1", MESHCORE_ADVERT_ON_START="false",
                   MESHCORE_CHUNK_DELAY="0", MESHCORE_RATE_LIMIT="off")
        env.update(self.env)
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self._patches = [mock.patch.object(A, name, value) for name, value in (
            ("ACK_WAIT_MIN", 0.3), ("ACK_WAIT_MAX", 0.3), ("IPC_INTERVAL", 0.05), ("STATE_INTERVAL", 0.2),
            ("WATCH_START_DELAY", 3600.0))]  # tests drive _watch_once directly
        for patch in self._patches:
            patch.start()
        gw_status.LOCKS.clear()
        self.adapter = A.MeshCoreAdapter(PlatformConfig(extra={}))

    async def asyncTearDown(self):
        await self.adapter.disconnect()
        await self.node.stop()
        for patch in self._patches:
            patch.stop()
        self._env.stop()
        self.tmp.cleanup()

    async def connect(self):
        self.assertTrue(await self.adapter.connect(), self.adapter.fatal_error_message)

    async def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        self.fail("condition not met in time")


class LifecycleTests(AdapterTestBase):
    async def test_connect_loads_node(self):
        await self.connect()
        self.assertEqual(self.adapter.node_name, "HermesBot")
        self.assertEqual(len(self.adapter._contacts), 4)
        self.assertEqual(self.adapter._channels, {0: "Public", 1: "Hermes"})
        self.assertIn(("meshcore", f"127.0.0.1:{self.node.port}"), gw_status.LOCKS)
        self.assertIs(registry.current(), self.adapter)
        self.assertEqual(self.node.sent(p.CMD_SEND_SELF_ADVERT), [])

    async def test_connect_failure_is_retryable(self):
        await self.node.stop()
        self.assertFalse(await self.adapter.connect())
        self.assertEqual(self.adapter.fatal_error_code, "connect_failed")
        self.assertTrue(self.adapter.fatal_error_retryable)
        self.assertEqual(gw_status.LOCKS, {})

    async def test_connect_failure_reaches_dashboard_state(self):
        await self.node.stop()
        self.assertFalse(await self.adapter.connect())
        snap = state.read_state()
        self.assertFalse(snap["connected"])
        self.assertEqual(snap["last_error"]["code"], "connect_failed")
        self.assertIn("refused", snap["last_error"]["message"])
        self.assertEqual(snap["host"], "127.0.0.1")

    def test_describe_error(self):
        self.assertIn("never answered", A.describe_error(p.MeshCoreTimeout("no reply from node")))
        self.assertIn("timed out", A.describe_error(asyncio.TimeoutError()))
        self.assertIn("refused", A.describe_error(ConnectionRefusedError()))

    async def test_success_clears_last_error(self):
        self.adapter._last_error = {"code": "connect_failed", "message": "x", "at": 1}
        await self.connect()
        self.assertIsNone(self.adapter.snapshot()["last_error"])

    async def test_missing_host_not_retryable(self):
        self.adapter.settings.host = ""
        self.assertFalse(await self.adapter.connect())
        self.assertFalse(self.adapter.fatal_error_retryable)

    async def test_lock_blocks_second_adapter(self):
        await self.connect()
        other = A.MeshCoreAdapter(PlatformConfig(extra={}))
        self.assertFalse(await other.connect())
        self.assertEqual(other.fatal_error_code, "meshcore_lock")
        await self.adapter.disconnect()
        self.assertEqual(gw_status.LOCKS, {})
        self.assertIsNone(registry.current())

    async def test_drop_notifies_gateway_once(self):
        await self.connect()
        self.node.drop_connections()
        await self.wait_for(lambda: self.adapter.fatal_notifications == 1)
        self.assertEqual(self.adapter.fatal_error_code, "connection_lost")
        self.assertTrue(self.adapter.fatal_error_retryable)
        await asyncio.sleep(0.1)
        self.assertEqual(self.adapter.fatal_notifications, 1)
        # The gateway then calls disconnect(); that must not notify again.
        await self.adapter.disconnect()
        self.assertEqual(self.adapter.fatal_notifications, 1)

    async def test_clean_disconnect_does_not_notify(self):
        await self.connect()
        await self.adapter.disconnect()
        await asyncio.sleep(0.05)
        self.assertEqual(self.adapter.fatal_notifications, 0)

    async def test_keepalive_silence_is_connection_loss(self):
        with mock.patch.object(A, "KEEPALIVE_INTERVAL", 0.05), mock.patch.object(A, "KEEPALIVE_TIMEOUT", 0.1):
            await self.connect()
            self.node.silent = True
            await self.wait_for(lambda: self.adapter.fatal_notifications == 1)
        self.assertIn("keepalive", self.adapter.fatal_error_message)

    async def test_advert_on_first_connect_only(self):
        self.adapter.settings.advert_on_start = True
        await self.connect()
        self.assertEqual(len(self.node.sent(p.CMD_SEND_SELF_ADVERT)), 1)
        await self.adapter.disconnect()
        self.assertTrue(await self.adapter.connect(is_reconnect=True))
        self.assertEqual(len(self.node.sent(p.CMD_SEND_SELF_ADVERT)), 1)


class InboundTests(AdapterTestBase):
    env = {"MESHCORE_ADMIN_NODES": fn.FakeNode.ALICE, "MESHCORE_REQUIRE_MENTION": "true"}

    async def test_dm_routed_with_name_and_colon_text(self):
        await self.connect()
        await self.node.deliver(p.RESP_CONTACT_MSG_V3, fn.contact_msg_body(BOB6, "time: what is it?"))
        await self.wait_for(lambda: self.adapter.events)
        ev = self.adapter.events[0]
        self.assertEqual(ev.text, "time: what is it?")
        self.assertEqual(ev.source.chat_id, f"dm:{BOB6}")
        self.assertEqual(ev.source.user_name, "Bob")
        self.assertFalse(ev.allow_gateway_control)
        self.assertIn('from "Bob"', ev.channel_prompt)
        self.assertIn("NOT your operator", ev.channel_prompt)
        self.assertFalse(getattr(ev, "metadata", None))  # Hermes refuses unknown metadata

    async def test_admin_dm(self):
        await self.connect()
        await self.node.deliver(p.RESP_CONTACT_MSG_V3, fn.contact_msg_body(ALICE6, "/status"))
        await self.wait_for(lambda: self.adapter.events)
        ev = self.adapter.events[0]
        self.assertTrue(ev.allow_gateway_control)
        self.assertIn("this is your operator", ev.channel_prompt)
        self.assertNotIn("NOT your operator", ev.channel_prompt)
        self.assertIsNone(self.adapter.toolsets_for_source(ev.source))

    async def test_cli_data_and_duplicates_ignored(self):
        await self.connect()
        await self.node.deliver(p.RESP_CONTACT_MSG_V3, fn.contact_msg_body(BOB6, "> ok", txt_type=1))
        body = fn.contact_msg_body(BOB6, "hello", ts=42)
        await self.node.deliver(p.RESP_CONTACT_MSG_V3, body)
        await self.node.deliver(p.RESP_CONTACT_MSG_V3, body)
        await self.wait_for(lambda: not self.node.inbox)
        await asyncio.sleep(0.1)
        self.assertEqual([e.text for e in self.adapter.events], ["hello"])

    async def test_channel_mention_gating(self):
        await self.connect()
        for text in ("Alice: just chatting", "Alice: @HermesBot weather?", "HermesBot: @HermesBot loop"):
            await self.node.deliver(p.RESP_CHANNEL_MSG_V3, fn.channel_msg_body(1, text, ts=hash(text) & 0xFFFF))
        await self.node.deliver(p.RESP_CHANNEL_MSG_V3, fn.channel_msg_body(0, "Bob: @HermesBot hi"))
        await self.wait_for(lambda: not self.node.inbox)
        await asyncio.sleep(0.1)
        self.assertEqual([e.text for e in self.adapter.events], ["weather?"])
        ev = self.adapter.events[0]
        self.assertEqual((ev.source.chat_id, ev.source.chat_type, ev.source.user_name), ("channel:1", "group", "Alice"))
        self.assertEqual(self.adapter.toolsets_for_source(ev.source), ["meshcore"])
        self.assertIn('from "Alice" in MeshCore channel Hermes', ev.channel_prompt)
        self.assertIn("several different people", ev.channel_prompt)

    async def test_dms_disabled(self):
        self.adapter.settings.enable_dms = False
        await self.connect()
        await self.node.deliver(p.RESP_CONTACT_MSG_V3, fn.contact_msg_body(BOB6, "hi"))
        await self.wait_for(lambda: not self.node.inbox)
        await asyncio.sleep(0.1)
        self.assertEqual(self.adapter.events, [])


class PolicyTests(AdapterTestBase):
    env = {"MESHCORE_ADMIN_NODES": fn.FakeNode.ALICE[:16], "MESHCORE_ADMIN_CHANNELS": "1",
           "MESHCORE_ALLOWED_USERS": "A1A1A1A1A1A1A1A1,b2b2b2b2,channel:1",
           "MESHCORE_PUBLIC_TOOLSETS": "meshcore,meshcore_admin,web"}

    def src(self, chat_type, chat_id, user_id=None):
        return self.adapter.build_source(chat_id=chat_id, chat_type=chat_type, user_id=user_id)

    async def test_toolsets(self):
        a = self.adapter
        self.assertIsNone(a.toolsets_for_source(self.src("dm", f"dm:{ALICE6}", ALICE6)))
        self.assertEqual(a.toolsets_for_source(self.src("dm", f"dm:{BOB6}", BOB6)), ["meshcore", "web"])
        self.assertIsNone(a.toolsets_for_source(self.src("group", "channel:1")))
        self.assertEqual(a.toolsets_for_source(self.src("group", "channel:0")), ["meshcore", "web"])
        self.assertFalse(a.is_admin_node("a1"))
        a.settings.admin_toolsets = ["terminal"]
        self.assertEqual(a.toolsets_for_source(self.src("dm", "x", ALICE6)), ["terminal", "meshcore", "meshcore_admin"])

    async def test_allowlist(self):
        self.assertEqual(self.adapter.config.extra["group_allowed_chats"], "channel:1")
        self.assertEqual(self.adapter.resolved_allowlist_user_ids(), {ALICE6})
        await self.connect()  # contacts let the short prefix resolve
        self.assertEqual(self.adapter.resolved_allowlist_user_ids(), {ALICE6, BOB6})


class OutboundTests(AdapterTestBase):
    async def test_dm_send_chunks(self):
        await self.connect()
        result = await self.adapter.send(f"dm:{BOB6}", "**Hello** " + "word " * 60)
        self.assertTrue(result.success, result.error)
        sent = self.node.sent(p.CMD_SEND_TXT_MSG)
        self.assertGreater(len(sent), 1)
        self.assertEqual(len(result.continuation_message_ids), len(sent) - 1)
        for payload in sent:
            self.assertEqual(payload[7:13], bytes.fromhex(BOB6))
            self.assertLessEqual(len(payload[13:]), 130)
        self.assertTrue(sent[0][13:].startswith(b"Hello word"))

    async def test_channel_send_accounts_for_name(self):
        await self.connect()
        result = await self.adapter.send("1", "x" * 300)
        self.assertTrue(result.success)
        for payload in self.node.sent(p.CMD_SEND_CHANNEL_TXT_MSG):
            self.assertEqual(payload[2], 1)
            self.assertLessEqual(len(payload[7:]) + len("HermesBot: "), 130)

    async def test_unknown_or_invalid_target(self):
        await self.connect()
        self.assertFalse((await self.adapter.send("dm:ffffffffffff", "hi")).success)
        self.assertFalse((await self.adapter.send("nonsense", "hi")).success)

    async def test_not_connected_is_retryable(self):
        result = await self.adapter.send("channel:1", "hi")
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    async def test_timeout_is_not_resent(self):
        await self.connect()
        self.node.silent = True
        with mock.patch.object(p.MeshCoreClient, "request", side_effect=p.MeshCoreTimeout("t")):
            result = await self.adapter.send("channel:1", "hi")
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)


class AdminQueryTests(AdapterTestBase):
    env = {"MESHCORE_ADMIN_NODES": fn.FakeNode.ALICE}

    async def test_status_is_fast_and_parsed(self):
        await self.connect()
        started = time.monotonic()
        result = await self.adapter.query_remote("Hilltop", "status")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["results"]["status"]["battery_mv"], 4012)
        self.assertEqual(len(self.node.sent(p.CMD_SEND_LOGIN)), 1)

    async def test_all_logs_in_once(self):
        await self.connect()
        result = await self.adapter.query_remote("hilltop rpt", "all")
        self.assertTrue(result["success"], result)
        self.assertEqual(set(result["results"]), {"status", "neighbours", "owner", "clock"})
        self.assertEqual(result["results"]["owner"]["name"], "Hilltop RPT")
        self.assertEqual(result["results"]["clock"]["remote_time"], 1700001234)
        self.assertEqual(len(self.node.sent(p.CMD_SEND_LOGIN)), 1)

    async def test_dm_during_query_is_delivered(self):
        await self.connect()

        async def hook(code, sub):
            await self.node.deliver(p.RESP_CONTACT_MSG_V3, fn.contact_msg_body(BOB6, "still there?"))
        self.node.on_binary_req = hook
        result = await self.adapter.query_remote("Hilltop RPT", "status")
        self.assertTrue(result["success"])
        await self.wait_for(lambda: self.adapter.events)
        self.assertEqual(self.adapter.events[0].text, "still there?")

    async def test_errors(self):
        await self.connect()
        self.assertIn("matches several", (await self.adapter.query_remote("RPT", "status"))["error"])
        self.assertIn("Unknown command", (await self.adapter.query_remote("Hilltop RPT", "ver"))["error"])
        flood = await self.adapter.query_remote("Valley RPT", "owner")
        self.assertFalse(flood["success"])
        self.assertIn("direct route", flood["error"])
        self.node.login_ok = False
        denied = await self.adapter.query_remote("Hilltop RPT", "status")
        self.assertIn("login rejected", denied["error"])

    async def test_disabled_without_admins(self):
        self.adapter.settings.admin_nodes = set()
        await self.connect()
        self.assertIn("disabled", (await self.adapter.query_remote("Hilltop RPT", "status"))["error"])


class IpcAndToolTests(AdapterTestBase):
    env = {"MESHCORE_ADMIN_NODES": fn.FakeNode.ALICE}

    async def test_state_snapshot(self):
        await self.connect()
        await self.wait_for(lambda: state.read_state().get("connected"))
        snap = state.read_state()
        self.assertTrue(snap["fresh"])
        self.assertEqual(snap["node"]["name"], "HermesBot")
        self.assertEqual(snap["contacts"]["repeaters"], 2)
        self.assertTrue(snap["config"]["admin_enabled"])
        mode = os.stat(state.state_dir() / "state.json").st_mode & 0o777
        self.assertEqual(mode, 0o600)

    async def test_ipc_admin_query_from_other_process(self):
        await self.connect()
        await self.wait_for(lambda: state.read_state().get("fresh"))
        # Simulate a CLI process: no live adapter visible.
        with mock.patch.object(registry, "current", return_value=None):
            out = json.loads(await tools.handle_admin({"node": "Hilltop RPT", "command": "status"}))
            self.assertTrue(out["success"], out)
            denied = json.loads(await tools.handle_admin({"node": "x", "command": "status", "password": "pw"}))
            self.assertFalse(denied["success"])
            contact = json.loads(await tools.handle_contact({"name": "bob"}))
        self.assertEqual(contact["pubkey_prefix"], BOB6)

    async def test_ipc_send_and_standalone(self):
        await self.connect()
        await self.wait_for(lambda: state.read_state().get("connected"))
        with mock.patch.object(registry, "current", return_value=None):
            out = await A._standalone_send(PlatformConfig(extra={}), "channel:1", "from cron")
        self.assertTrue(out.get("success"), out)
        self.assertTrue(any(b"from cron" in x for x in self.node.sent(p.CMD_SEND_CHANNEL_TXT_MSG)))

    async def test_live_tools(self):
        await self.connect()
        out = json.loads(await tools.handle_contact({"name": "Hilltop"}))
        self.assertEqual(out["type"], "repeater")
        self.assertTrue(tools.admin_available())

    async def test_profile_isolation(self):
        await self.connect()
        with tempfile.TemporaryDirectory() as other:
            with mock.patch.dict(os.environ, {"HERMES_HOME": other}):
                self.assertIsNone(registry.current())
                self.assertFalse(state.read_state().get("connected"))
                self.assertFalse(tools.admin_available())
        self.assertIs(registry.current(), self.adapter)


class StandaloneDirectTests(AdapterTestBase):
    async def test_direct_session_when_no_gateway(self):
        out = await A._standalone_send(PlatformConfig(extra={}), f"dm:{BOB6}", "direct")
        self.assertTrue(out.get("success"), out)
        self.assertEqual(len(self.node.sent(p.CMD_SEND_TXT_MSG)), 1)


class RegistrationTests(unittest.TestCase):
    def test_register(self):
        from _load import pkg

        class Ctx:
            def __init__(self):
                self.platform, self.tools = None, []

            def register_platform(self, **kw):
                self.platform = kw

            def register_tool(self, **kw):
                self.tools.append(kw)

        ctx = Ctx()
        pkg.register(ctx)
        self.assertEqual(ctx.platform["name"], "meshcore")
        self.assertEqual(ctx.platform["max_message_length"], 0)
        self.assertIs(ctx.platform["standalone_sender_fn"], A._standalone_send)
        self.assertNotIn("dashboard_description", ctx.platform)
        self.assertEqual(ctx.platform["display_tier"], "minimal")
        self.assertFalse(ctx.platform["display_overrides"]["busy_steer_ack_enabled"])  # not a field in the stub registry
        self.assertEqual([t["name"] for t in ctx.tools], ["meshcore_contact", "meshcore_nearby", "meshcore_admin"])

    def test_env_enablement(self):
        env = {"MESHCORE_HOST": "n", "MESHCORE_PORT": "4000", "MESHCORE_HOME_CHANNEL": "2"}
        with mock.patch.dict(os.environ, env, clear=True):
            seed = A._env_enablement()
        self.assertEqual(seed["port"], 4000)
        self.assertEqual(seed["home_channel"]["chat_id"], "channel:2")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(A._env_enablement())


if __name__ == "__main__":
    unittest.main()
