"""Security tests for MeshCore plugin: MESH-001 and MESH-002.

Exercises toolset authorization, command allowlist rejection, and defense-in-depth
auth checks without requiring a live TCP connection.
"""

import json

import adapter
from adapter import (
    MeshCoreAdapter,
    _check_admin_auth,
    _handle_meshcore_admin,
    _handle_meshcore_admin_query,
    _handle_meshcore_contact,
    _parse_channel_index,
)


# ── Unit: channel index parsing ────────────────────────────────────

def test_parse_channel_index_valid():
    assert _parse_channel_index("channel:3") == 3
    assert _parse_channel_index("channel:7") == 7
    assert _parse_channel_index("channel:0") == 0


def test_parse_channel_index_invalid():
    assert _parse_channel_index("dm:abc123") is None
    assert _parse_channel_index("channel:") is None
    assert _parse_channel_index("") is None
    assert _parse_channel_index("channel:abc") is None


# ── Unit: toolsets_for_source ──────────────────────────────────────

class FakeSource:
    """Minimal fake matching the fields toolsets_for_source reads."""
    def __init__(self, chat_type, chat_id, user_id=None):
        self.chat_type = chat_type
        self.chat_id = chat_id
        self.user_id = user_id


def _make_adapter(admin_nodes=None, admin_channels=None):
    """Build a MeshCoreAdapter with fake config for testing."""
    class FakeConfig:
        extra = {}
    import gateway.platforms.base as base_mod
    import gateway.config as config_mod
    cfg = FakeConfig()
    plat = config_mod.Platform("meshcore")
    obj = object.__new__(MeshCoreAdapter)
    base_mod.BasePlatformAdapter.__init__(obj, config=cfg, platform=plat)
    obj._conn = None
    obj.admin_nodes = admin_nodes or set()
    obj.admin_channels = admin_channels or set()
    return obj


def test_toolsets_admin_dm():
    adapter = _make_adapter(admin_nodes={"abc123"})
    src = FakeSource("dm", "dm:abc123", user_id="abc123")
    result = adapter.toolsets_for_source(src)
    assert "meshcore" in result
    assert "meshcore_admin" in result


def test_toolsets_unauthorized_dm():
    adapter = _make_adapter(admin_nodes={"abc123"})
    src = FakeSource("dm", "dm:xyz789", user_id="xyz789")
    result = adapter.toolsets_for_source(src)
    assert result == ["meshcore"]


def test_toolsets_admin_channel():
    adapter = _make_adapter(admin_channels={3})
    src = FakeSource("channel", "channel:3")
    result = adapter.toolsets_for_source(src)
    assert "meshcore" in result
    assert "meshcore_admin" in result


def test_toolsets_public_channel():
    adapter = _make_adapter(admin_channels={3})
    src = FakeSource("channel", "channel:7")
    result = adapter.toolsets_for_source(src)
    assert result == ["meshcore"]


def test_toolsets_no_admin_config():
    """When no admin_nodes or admin_channels are set, all sources are restricted."""
    adapter = _make_adapter()
    src = FakeSource("dm", "dm:abc123", user_id="abc123")
    result = adapter.toolsets_for_source(src)
    assert result == ["meshcore"], (
        "Without admin_nodes/admin_channels configured, even a DM should be restricted"
    )


def test_toolsets_group_with_admin_channel():
    adapter = _make_adapter(admin_channels={5})
    src = FakeSource("group", "channel:5")
    result = adapter.toolsets_for_source(src)
    assert "meshcore_admin" in result


def test_toolsets_parse_failure_fail_closed():
    adapter = _make_adapter(admin_nodes={"abc"})
    # Missing chat_type
    src = FakeSource(None, None)
    result = adapter.toolsets_for_source(src)
    assert result == ["meshcore"]


# ── Unit: defense-in-depth auth check ──────────────────────────────

def test_check_admin_auth_no_instance():
    MeshCoreAdapter._instance = None
    result = _check_admin_auth()
    assert result is not None
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "not connected" in parsed["error"]


def test_check_admin_auth_no_admin_config():
    adapter = _make_adapter()
    MeshCoreAdapter._instance = adapter
    try:
        result = _check_admin_auth()
        assert result is not None
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "not authorized" in parsed["error"]
    finally:
        MeshCoreAdapter._instance = None


def test_check_admin_auth_authorized():
    adapter = _make_adapter(admin_nodes={"abc"})
    MeshCoreAdapter._instance = adapter
    try:
        result = _check_admin_auth()
        assert result is None
    finally:
        MeshCoreAdapter._instance = None


# ── Unit: handler auth gate ────────────────────────────────────────

def test_admin_handler_rejected_no_instance():
    MeshCoreAdapter._instance = None
    try:
        import asyncio
        result = asyncio.run(
            _handle_meshcore_admin("test-node", "ver")
        )
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "not connected" in parsed["error"]
    finally:
        MeshCoreAdapter._instance = None


def test_admin_handler_rejected_no_admin_config():
    adapter = _make_adapter()
    MeshCoreAdapter._instance = adapter
    try:
        import asyncio
        result = asyncio.run(
            _handle_meshcore_admin("test-node", "ver")
        )
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "not authorized" in parsed["error"]
    finally:
        MeshCoreAdapter._instance = None


def test_admin_query_handler_requires_running_gateway():
    """A separate session checks gateway state, not a local singleton."""
    MeshCoreAdapter._instance = None
    import asyncio
    from unittest.mock import patch
    from tempfile import TemporaryDirectory
    from pathlib import Path
    with TemporaryDirectory() as tmp, patch("adapter.get_profile_scoped_dir", return_value=Path(tmp)):
        result = asyncio.run(_handle_meshcore_admin_query("test-node", "ver"))
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "no state file" in parsed["error"]


def test_admin_query_rejects_password_bearing_ipc():
    """Cross-process admin queries must not accept a password."""
    instance = _make_adapter(admin_nodes={"admin-node"})
    MeshCoreAdapter._instance = instance
    try:
        import asyncio
        result = asyncio.run(
            _handle_meshcore_admin_query(
                "test-node", "ver", password="query-password-sentinel"
            )
        )
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "not supported" in parsed["error"]
    finally:
        MeshCoreAdapter._instance = None


def test_contact_handler_no_auth_required():
    """meshcore_contact should remain accessible — no admin auth required."""
    MeshCoreAdapter._instance = None
    import asyncio
    result = asyncio.run(
        _handle_meshcore_contact("test-node")
    )
    parsed = json.loads(result)
    assert parsed["success"] is False  # No adapter — but no auth error
    assert "not connected" in parsed["error"] or "gateway" in parsed.get("error", "")


# ── Unit: command allowlist rejection (MESH-002) ───────────────────

def test_query_remote_repeater_rejects_unknown_command():
    """Unknown commands are NOT sent as text DMs — they are rejected immediately."""
    import asyncio

    class FakeConn:
        is_connected = True
        async def send_command(self, cmd, allowed_types, timeout=5.0):
            return (0x06, b"dummy")  # PKT_MSG_SENT

    adapter = _make_adapter()
    adapter._conn = FakeConn()
    adapter._admin_query_lock = asyncio.Lock()
    adapter._admin_query_target = ""
    adapter._admin_query_responses = []
    adapter._contacts = {
        "test-node": {
            "public_key": "a" * 64,
            "adv_name": "test-node",
            "type": 2,
            "adv_lat": None,
            "adv_lon": None,
            "last_advert": 0,
        }
    }

    # "reboot" is not in BINARY_COMMAND_MAP — must be rejected
    result = asyncio.run(
        adapter.query_remote_repeater("test-node", "reboot")
    )
    assert result["success"] is False
    assert "Unknown command" in result["error"]
    assert "reboot" in result["error"]


def test_query_remote_repeater_accepts_known_command():
    """Known read-only commands are accepted."""
    import asyncio

    class FakeConn:
        is_connected = True
        async def send_command(self, cmd, allowed_types, timeout=5.0):
            return (0x06, b"dummy")  # PKT_MSG_SENT

    adapter = _make_adapter()
    adapter._conn = FakeConn()
    adapter._admin_query_lock = asyncio.Lock()
    adapter._admin_query_target = ""
    adapter._admin_query_responses = []
    adapter._contacts = {
        "test-node": {
            "public_key": "a" * 64,
            "adv_name": "test-node",
            "type": 2,
            "adv_lat": None,
            "adv_lon": None,
            "last_advert": 0,
        }
    }

    # "ver" IS in BINARY_COMMAND_MAP — should be accepted (login happens)
    result = asyncio.run(
        adapter.query_remote_repeater("test-node", "ver")
    )
    # The login portion will likely fail since we're using a fake connection,
    # but the error should NOT be "Unknown command"
    assert "Unknown command" not in result.get("error", "")
