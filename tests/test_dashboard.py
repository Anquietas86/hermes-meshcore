"""Dashboard API: loads standalone (as Hermes does) and validates input."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _load  # noqa: F401  (puts the stubs on sys.path)

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    FastAPI = None

API = Path(__file__).resolve().parent.parent / "dashboard" / "api.py"


@unittest.skipIf(FastAPI is None, "fastapi not installed")
class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        self._env.start()
        spec = importlib.util.spec_from_file_location("meshcore_dashboard_api_test", API)
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        app = FastAPI()
        app.include_router(self.api.router, prefix="/api/plugins/meshcore-platform")
        self.client = TestClient(app)
        self.env_file = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def url(self, path):
        return f"/api/plugins/meshcore-platform{path}"

    def test_status_without_gateway(self):
        r = self.client.get(self.url("/status"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["connected"])
        self.assertEqual(self.client.get(self.url("/contacts")).status_code, 503)

    def test_config_roundtrip_preserves_other_lines(self):
        self.env_file.write_text("OPENAI_API_KEY=sk-x\nMESHCORE_MONITOR_CHANNELS=0\n")
        r = self.client.post(self.url("/config"), json={"monitor_channels": "1, 2", "enable_dms": False})
        self.assertEqual(r.status_code, 200, r.text)
        text = self.env_file.read_text()
        self.assertIn("OPENAI_API_KEY=sk-x\n", text)
        self.assertIn("MESHCORE_MONITOR_CHANNELS=1,2\n", text)
        self.assertIn("MESHCORE_ENABLE_DMS=false\n", text)
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.client.get(self.url("/config")).json()["monitor_channels"], "1,2")

    def test_config_rejects_injection_and_unknown_keys(self):
        for body in ({"monitor_channels": "1\nOPENAI_API_KEY=evil"}, {"admin_nodes": "zz"},
                     {"enable_dms": "maybe"}, {"debug": "true"}, {}):
            r = self.client.post(self.url("/config"), json=body)
            self.assertEqual(r.status_code, 400, body)
        self.assertFalse(self.env_file.exists())

    def test_clearing_a_value_removes_the_line(self):
        self.env_file.write_text("MESHCORE_ADMIN_CHANNELS=3\n")
        self.client.post(self.url("/config"), json={"admin_channels": ""})
        self.assertNotIn("MESHCORE_ADMIN_CHANNELS", self.env_file.read_text())

    def test_admin_query_guards(self):
        r = self.client.post(self.url("/admin/query"), json={"node": "x", "command": "status", "password": "p"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(self.url("/admin/query"), json={"node": "x", "command": "status"})
        self.assertEqual(r.status_code, 503)
        self.api.state.write_state({"connected": True, "config": {"admin_enabled": True}})
        r = self.client.post(self.url("/admin/query"), json={"node": "x", "command": "status"})
        self.assertEqual(r.status_code, 200)
        rid = r.json()["request_id"]
        pending = self.client.get(self.url("/admin/result"), params={"request_id": rid}).json()
        self.assertEqual(pending, {"status": "pending", "queued": True})
        self.api.state.write_response(rid, {"success": True})
        done = self.client.get(self.url("/admin/result"), params={"request_id": rid}).json()
        self.assertEqual(done["status"], "complete")
        bad = self.client.get(self.url("/admin/result"), params={"request_id": "../../etc"})
        self.assertEqual(bad.status_code, 400)

    def test_channel_endpoints(self):
        r = self.client.post(self.url("/channels"), json={"index": 2, "name": "#x"})
        self.assertEqual(r.status_code, 503)
        self.api.state.write_state({"connected": True, "max_channels": 8, "channels": {"0": "Public"}})
        for body in ({"index": 9, "name": "#x"}, {"index": "a", "name": "#x"}, {"index": 2, "name": ""},
                     {"index": 2, "name": "a\nb"}, {"index": 2, "name": "x", "channel_key": "zz"}):
            self.assertEqual(self.client.post(self.url("/channels"), json=body).status_code, 400, body)
        ok = self.client.post(self.url("/channels"), json={"index": 2, "name": "Family", "channel_key": "AB" * 16})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.client.post(self.url("/channels/remove"), json={"index": 0}).status_code, 200)
        listed = self.client.get(self.url("/channels")).json()
        self.assertEqual((listed["channels"], listed["max_channels"]), ({"0": "Public"}, 8))

    def test_node_connection_fields(self):
        r = self.client.post(self.url("/config"), json={"host": "192.168.1.50", "port": "5000"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("MESHCORE_HOST=192.168.1.50\n", self.env_file.read_text())
        self.client.post(self.url("/config"), json={"host": "serial:/dev/ttyUSB0"})
        self.assertEqual(self.client.get(self.url("/config")).json()["host"], "serial:/dev/ttyUSB0")
        for body in ({"host": "a b;rm"}, {"host": "x\nOPENAI_API_KEY=1"}, {"port": "0"}, {"port": "70000"},
                     {"port": "50a"}, {"host": "serial:/etc/../x y"}):
            self.assertEqual(self.client.post(self.url("/config"), json=body).status_code, 400, body)

    def test_connection_test(self):
        import asyncio
        import threading
        import fakenode as fn
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        node = asyncio.run_coroutine_threadsafe(fn.FakeNode().start(), loop).result(5)
        try:
            ok = self.client.post(self.url("/test-connection"), json={"host": "127.0.0.1", "port": node.port}).json()
            self.assertTrue(ok["success"], ok)
            self.assertEqual(ok["node_name"], "HermesBot")
            asyncio.run_coroutine_threadsafe(node.stop(), loop).result(5)
            bad = self.client.post(self.url("/test-connection"), json={"host": "127.0.0.1", "port": node.port}).json()
            self.assertFalse(bad["success"])
            self.assertIn("refused", bad["error"])
            self.api.state.write_state({"connected": True, "host": "127.0.0.1", "port": 5000})
            busy = self.client.post(self.url("/test-connection"), json={}).json()
            self.assertTrue(busy["skipped"])
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
        self.api.state.write_state({"connected": False})
        self.assertEqual(self.client.post(self.url("/test-connection"), json={}).status_code, 400)

    def test_finds_the_profile_running_meshcore(self):
        root = Path(self.tmp.name)
        mesh = root / "profiles" / "meshcore"
        mesh.mkdir(parents=True)
        (root / "profiles" / "work").mkdir()
        self.api.state.write_state({"connected": True, "host": "10.0.0.9", "port": 5000}, mesh)
        status = self.client.get(self.url("/status")).json()
        self.assertTrue(status["connected"])
        self.assertEqual(status["profile"], "meshcore")
        self.client.post(self.url("/config"), json={"monitor_channels": "3"})
        self.assertIn("MESHCORE_MONITOR_CHANNELS=3", (mesh / ".env").read_text())
        self.assertFalse(self.env_file.exists())
        # Once the active profile has its own MeshCore state, it wins.
        self.api.state.write_state({"connected": False}, root)
        self.assertEqual(self.client.get(self.url("/status")).json()["profile"], "default")


if __name__ == "__main__":
    unittest.main()
