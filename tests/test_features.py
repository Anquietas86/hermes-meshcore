"""Pacing, DM acknowledgements, rate limits, channel context, nearby, repeater watch, serial."""

import asyncio
import json
import os
import struct
import sys
import time
import unittest
from unittest import mock

from _load import adapter as A, protocol as p, textutil, tools
import fakenode as fn
from test_adapter import ALICE6, BOB6, AdapterTestBase

geo = sys.modules["meshcore_platform.geo"]


class HelperTests(unittest.TestCase):
    def test_parse_ack(self):
        self.assertEqual(p.parse_ack(b"\x01\x02\x03\x04" + struct.pack("<I", 900)), {"code": "01020304", "trip_ms": 900})
        self.assertEqual(p.parse_ack(b"\xaa\xbb\xcc\xdd"), {"code": "aabbccdd"})

    def test_airtime(self):
        slow = p.lora_airtime_s(154, 11, 250, 5)
        self.assertTrue(1.0 < slow < 2.0, slow)
        self.assertLess(p.lora_airtime_s(154, 7, 250, 5), slow)
        self.assertGreater(p.lora_airtime_s(154, 12, 125, 8), slow)
        self.assertAlmostEqual(p.lora_airtime_s(154, 0, 0, 0), slow, places=3)  # unknown radio = SF11/250

    def test_reset_path(self):
        self.assertEqual(p.cmd_reset_path(b"\x01" * 32)[0], p.CMD_RESET_PATH)
        with self.assertRaises(ValueError):
            p.cmd_reset_path(b"\x01" * 6)

    def test_parse_rate(self):
        self.assertEqual(textutil.parse_rate("6/10m"), (6, 600))
        self.assertEqual(textutil.parse_rate("3/1h"), (3, 3600))
        self.assertEqual(textutil.parse_rate("5/90"), (5, 90))
        self.assertIsNone(textutil.parse_rate("off"))
        self.assertEqual(textutil.parse_rate(""), (6, 600))
        self.assertEqual(textutil.parse_rate("junk"), (6, 600))

    def test_geo(self):
        contacts = [{"name": "far", "type": "repeater", "lat": -35.5, "lon": 138.6},
                    {"name": "near", "type": "repeater", "lat": -34.93, "lon": 138.62},
                    {"name": "someone", "type": "client", "lat": -34.92, "lon": 138.6},
                    {"name": "unplaced", "type": "repeater", "lat": 0.0, "lon": 0.0}]
        out = geo.nearby(-34.928, 138.6, contacts)
        self.assertEqual([n["name"] for n in out["nodes"]], ["near", "far"])
        self.assertEqual(out["without_location"], 1)
        self.assertEqual(out["nodes"][1]["direction"], "S")
        self.assertAlmostEqual(out["nodes"][1]["distance_km"], 63.6, delta=0.5)
        self.assertEqual(len(geo.nearby(-34.928, 138.6, contacts, kind="any")["nodes"]), 3)
        self.assertFalse(geo.nearby(0, 0, contacts)["success"])


class SendingTests(AdapterTestBase):
    async def test_dm_parts_wait_for_acks(self):
        await self.connect()
        result = await self.adapter.send(f"dm:{BOB6}", "word " * 60)
        self.assertTrue(result.success)
        sent = self.node.sent(p.CMD_SEND_TXT_MSG)
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(x[2] == 0 for x in sent))  # no retries
        self.assertEqual(result.raw_response["unacknowledged_parts"], [])
        self.assertEqual(self.node.sent(p.CMD_RESET_PATH), [])

    async def test_missing_ack_retries_once_over_flood(self):
        await self.connect()
        self.node.ack_attempts = {1}
        result = await self.adapter.send(f"dm:{BOB6}", "hello")
        self.assertTrue(result.success)
        attempts = [x[2] for x in self.node.sent(p.CMD_SEND_TXT_MSG)]
        self.assertEqual(attempts, [0, 1])
        stamps = {x[3:7] for x in self.node.sent(p.CMD_SEND_TXT_MSG)}
        self.assertEqual(len(stamps), 1)  # same timestamp so the receiver can spot the repeat
        self.assertEqual(len(self.node.sent(p.CMD_RESET_PATH)), 1)
        self.assertEqual(result.raw_response["unacknowledged_parts"], [])

    async def test_no_ack_at_all_is_reported(self):
        await self.connect()
        self.node.dm_acks = False
        result = await self.adapter.send(f"dm:{BOB6}", "hello")
        self.assertTrue(result.success)  # the node did transmit it
        self.assertEqual(result.raw_response["unacknowledged_parts"], [1])
        self.assertEqual(len(self.node.sent(p.CMD_SEND_TXT_MSG)), 2)

    async def test_late_ack_for_first_attempt_counts(self):
        await self.connect()
        self.node.ack_attempts = {0}
        self.node.ack_delay = 0.4  # arrives after the first wait, during the retry
        result = await self.adapter.send(f"dm:{BOB6}", "hello")
        self.assertEqual(result.raw_response["unacknowledged_parts"], [])

    async def test_concurrent_replies_do_not_interleave(self):
        await self.connect()
        self.adapter.settings.chunk_delay = 0.02
        await asyncio.gather(self.adapter.send("channel:1", "a" * 300), self.adapter.send("channel:1", "b" * 300))
        texts = [x[7:8] for x in self.node.sent(p.CMD_SEND_CHANNEL_TXT_MSG)]
        self.assertEqual(texts, sorted(texts) if texts[0] == b"a" else sorted(texts, reverse=True))

    async def test_auto_gap_follows_airtime(self):
        await self.connect()
        self.adapter.settings.chunk_delay = None
        self.assertEqual(self.adapter._chunk_gap(130, acked=True), A.ACKED_CHUNK_GAP)
        gap = self.adapter._chunk_gap(130, acked=False)
        si = self.adapter._self_info
        expected = 3 * p.lora_airtime_s(130 + p.PACKET_OVERHEAD_BYTES, si["radio_sf"], si["radio_bw"], si["radio_cr"])
        self.assertAlmostEqual(gap, max(A.MIN_CHUNK_GAP, expected))
        self.assertGreaterEqual(gap, A.MIN_CHUNK_GAP)


class RateLimitTests(AdapterTestBase):
    env = {"MESHCORE_RATE_LIMIT": "2/1m", "MESHCORE_ADMIN_NODES": fn.FakeNode.ALICE}

    async def test_dm_rate_limit_with_one_notice(self):
        await self.connect()
        for i in range(4):
            await self.node.deliver(p.RESP_CONTACT_MSG_V3, fn.contact_msg_body(BOB6, f"msg {i}", ts=100 + i))
        await self.wait_for(lambda: not self.node.inbox)
        await asyncio.sleep(0.3)
        self.assertEqual([e.text for e in self.adapter.events], ["msg 0", "msg 1"])
        notices = [x for x in self.node.sent(p.CMD_SEND_TXT_MSG) if b"Too many" in x]
        self.assertEqual(len(notices), 1)

    async def test_admin_exempt(self):
        await self.connect()
        for i in range(4):
            await self.node.deliver(p.RESP_CONTACT_MSG_V3, fn.contact_msg_body(ALICE6, f"msg {i}", ts=200 + i))
        await self.wait_for(lambda: len(self.adapter.events) == 4)

    async def test_channel_limit_is_silent(self):
        await self.connect()
        for i in range(4):
            await self.node.deliver(p.RESP_CHANNEL_MSG_V3, fn.channel_msg_body(1, f"Eve: hi {i}", ts=300 + i))
        await self.wait_for(lambda: not self.node.inbox)
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.adapter.events), 2)
        self.assertEqual(self.node.sent(p.CMD_SEND_CHANNEL_TXT_MSG), [])


class ChannelContextTests(AdapterTestBase):
    env = {"MESHCORE_REQUIRE_MENTION": "true"}

    async def test_mention_gets_recent_chatter(self):
        await self.connect()
        for i, text in enumerate(("Bob: is the hilltop repeater down?", "Carol: seems so, no adverts",
                                  "Bob: @HermesBot what do you think?")):
            await self.node.deliver(p.RESP_CHANNEL_MSG_V3, fn.channel_msg_body(1, text, ts=400 + i))
        await self.wait_for(lambda: self.adapter.events)
        ev = self.adapter.events[0]
        self.assertEqual(ev.text, "what do you think?")
        self.assertIn("Bob: is the hilltop repeater down?", ev.channel_context)
        self.assertIn("Carol: seems so, no adverts", ev.channel_context)
        self.assertNotIn("what do you think", ev.channel_context)
        self.assertIn("context only", ev.channel_context)

    async def test_bot_replies_are_part_of_context(self):
        await self.connect()
        await self.adapter.send("channel:1", "Hilltop last advertised 3h ago.")
        await self.node.deliver(p.RESP_CHANNEL_MSG_V3, fn.channel_msg_body(1, "Bob: @HermesBot and Valley?", ts=500))
        await self.wait_for(lambda: self.adapter.events)
        self.assertIn("HermesBot: Hilltop last advertised", self.adapter.events[0].channel_context)

    async def test_disabled(self):
        self.adapter.settings.context_messages = 0
        await self.connect()
        await self.node.deliver(p.RESP_CHANNEL_MSG_V3, fn.channel_msg_body(1, "Bob: chatter", ts=600))
        await self.node.deliver(p.RESP_CHANNEL_MSG_V3, fn.channel_msg_body(1, "Bob: @HermesBot hi", ts=601))
        await self.wait_for(lambda: self.adapter.events)
        self.assertIsNone(self.adapter.events[0].channel_context)


class NearbyTests(AdapterTestBase):
    async def test_live_and_from_state(self):
        await self.connect()
        live = json.loads(await tools.handle_nearby({}))
        self.assertEqual([n["name"] for n in live["nodes"]], ["Hilltop RPT", "Valley RPT"])
        self.assertEqual(live["nodes"][1]["direction"], "S")
        await self.wait_for(lambda: os.path.exists(os.path.join(self.tmp.name, ".meshcore", "state.json")))
        with mock.patch.object(tools.registry, "current", return_value=None):
            await asyncio.sleep(0.3)
            cached = json.loads(await tools.handle_nearby({"type": "repeater", "limit": 1}))
        self.assertEqual([n["name"] for n in cached["nodes"]], ["Hilltop RPT"])


class WatchTests(AdapterTestBase):
    env = {"MESHCORE_ADMIN_NODES": fn.FakeNode.ALICE, "MESHCORE_WATCH_NODES": "Hilltop RPT",
           "MESHCORE_WATCH_BATTERY_MV": "4100"}

    def alerts(self):
        return [x[13:].decode() for x in self.node.sent(p.CMD_SEND_TXT_MSG) if x[7:13] == bytes.fromhex(ALICE6)]

    async def test_low_battery_alerts_once_then_recovers(self):
        await self.connect()
        await self.adapter._watch_once()
        await self.adapter._watch_once()
        self.assertEqual(self.alerts(), ["Watch: Hilltop RPT - battery low (4.01 V)"])
        self.adapter.settings.watch_battery_mv = 3500
        await self.adapter._watch_once()
        self.assertEqual(self.alerts()[-1], "Watch: Hilltop RPT is OK again")
        snap = self.adapter.snapshot()["watch"][0]
        self.assertEqual((snap["name"], snap["battery_mv"]), ("Hilltop RPT", 4012))

    async def test_silent_repeater(self):
        self.adapter.settings.watch_status = False
        await self.connect()
        await self.adapter._watch_once()
        self.assertEqual(self.alerts(), [])
        self.adapter._watch["Hilltop RPT"]["advert_seen_at"] -= 13 * 3600
        await self.adapter._watch_once()
        self.assertEqual(self.alerts(), ["Watch: Hilltop RPT - no advert heard for 13h"])

    async def test_notify_target_and_loop_started(self):
        self.adapter.settings.watch_notify = "channel:1"
        self.adapter.settings.watch_status = False
        await self.connect()
        self.assertIn("meshcore-watch", [t.get_name() for t in self.adapter._tasks])
        self.adapter._watch["x"] = {"query": "x", "name": "Hilltop RPT", "problems": {}}
        await self.adapter._watch_report(self.adapter._watch["x"], {"silent": "no advert heard for 20h"})
        self.assertTrue(any(b"no advert heard" in x for x in self.node.sent(p.CMD_SEND_CHANNEL_TXT_MSG)))


@unittest.skipUnless(hasattr(os, "openpty"), "needs a pty")
class SerialTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_over_a_pty(self):
        master, slave = os.openpty()
        path = os.ttyname(slave)
        loop = asyncio.get_running_loop()
        received = bytearray()

        def node_side():
            received.extend(os.read(master, 1024))
            os.write(master, fn.frame(b"\x05" + fn.self_info_body("SerialBot")))
        client = p.MeshCoreClient("serial:" + path, 0)
        try:
            await client.open()
            loop.add_reader(master, node_side)
            reply = await client.request(p.cmd_app_start(), [p.RESP_SELF_INFO], timeout=3)
            self.assertEqual(p.parse_self_info(reply.body)["name"], "SerialBot")
            self.assertEqual(bytes(received[:4]), p.encode_frame(p.cmd_app_start())[:4])
        finally:
            loop.remove_reader(master)
            await client.close()
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
