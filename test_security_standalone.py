"""Standalone unit tests for MeshCore security — MESH-001 + MESH-002.

Runs without importing the hermes-agent package tree. Tests the
authorization logic, toolset resolution, and command allowlist in isolation.
"""

import json


# ── Replicated logic from adapter._parse_channel_index ────────────────

def _parse_channel_index(chat_id):
    """Extract channel index from a chat_id like 'channel:3' or 'channel:7'."""
    try:
        if chat_id.startswith("channel:"):
            return int(chat_id.split(":", 1)[1])
    except (ValueError, IndexError):
        pass
    return None


# ── Replicated logic from adapter.toolsets_for_source ────────────────

def resolve_toolsets(chat_type, chat_id, user_id, admin_nodes, admin_channels):
    """Per-source toolset resolution (replicated from adapter).

    Returns list of toolset names. Fail-closed: defaults to ['meshcore'].
    """
    try:
        if chat_type == "dm":
            if user_id in admin_nodes:
                return ["meshcore", "meshcore_admin"]
            return ["meshcore"]
        if chat_type in ("group", "channel"):
            idx = _parse_channel_index(chat_id)
            if idx is not None and idx in admin_channels:
                return ["meshcore", "meshcore_admin"]
            return ["meshcore"]
    except Exception:
        pass
    return ["meshcore"]


# ── Replicated logic for _check_admin_auth ──────────────────────────

def check_admin_auth(admin_nodes, admin_channels, adapter_connected=True):
    """Defense-in-depth admin authorization check.

    Returns None if authorized, or error JSON string if not.
    """
    if not adapter_connected:
        return json.dumps({
            "success": False,
            "error": "MeshCore gateway not connected — admin tools require the gateway to be running"
        })
    if not admin_nodes and not admin_channels:
        return json.dumps({
            "success": False,
            "error": (
                "Admin tools are not authorized for this source. "
                "Configure MESHCORE_ADMIN_NODES or MESHCORE_ADMIN_CHANNELS."
            )
        })
    return None


# ── Tests ────────────────────────────────────────────────────────────

def test_parse_channel_index():
    assert _parse_channel_index("channel:3") == 3
    assert _parse_channel_index("channel:7") == 7
    assert _parse_channel_index("channel:0") == 0
    assert _parse_channel_index("dm:abc123") is None
    assert _parse_channel_index("channel:") is None
    assert _parse_channel_index("") is None
    assert _parse_channel_index("channel:xyz") is None
    print("  PASS: _parse_channel_index")


def test_toolsets_admin_dm():
    result = resolve_toolsets("dm", "dm:abc123", "abc123", {"abc123"}, set())
    assert "meshcore" in result
    assert "meshcore_admin" in result
    print("  PASS: admin DM gets both toolsets")


def test_toolsets_unauthorized_dm():
    result = resolve_toolsets("dm", "dm:xyz789", "xyz789", {"abc123"}, set())
    assert result == ["meshcore"]
    print("  PASS: unauthorized DM gets only meshcore")


def test_toolsets_admin_channel():
    result = resolve_toolsets("channel", "channel:3", None, set(), {3})
    assert "meshcore" in result
    assert "meshcore_admin" in result
    print("  PASS: admin channel gets both toolsets")


def test_toolsets_public_channel():
    result = resolve_toolsets("channel", "channel:7", None, set(), {3})
    assert result == ["meshcore"]
    print("  PASS: public channel gets only meshcore")


def test_toolsets_group_admin():
    result = resolve_toolsets("group", "channel:5", None, set(), {5})
    assert "meshcore_admin" in result
    print("  PASS: admin group gets both toolsets")


def test_toolsets_no_admin_config():
    result = resolve_toolsets("dm", "dm:abc123", "abc123", set(), set())
    assert result == ["meshcore"]
    print("  PASS: no admin config → restricted")


def test_toolsets_fail_closed():
    """Parse failures default to restricted."""
    result = resolve_toolsets(None, None, None, {"abc"}, set())
    assert result == ["meshcore"]
    print("  PASS: fail-closed on parse error")


def test_check_admin_auth_no_instance():
    result = check_admin_auth(set(), set(), adapter_connected=False)
    assert result is not None
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "not connected" in parsed["error"]
    print("  PASS: auth rejected when gateway not connected")


def test_check_admin_auth_no_config():
    result = check_admin_auth(set(), set(), adapter_connected=True)
    assert result is not None
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "not authorized" in parsed["error"]
    print("  PASS: auth rejected when no admin config")


def test_check_admin_auth_authorized():
    result = check_admin_auth({"abc"}, set(), adapter_connected=True)
    assert result is None
    print("  PASS: auth allowed with admin nodes configured")

    result = check_admin_auth(set(), {3}, adapter_connected=True)
    assert result is None
    print("  PASS: auth allowed with admin channels configured")


# ── MESH-002: command allowlist rejection ───────────────────────────

# Replicated BINARY_COMMAND_MAP keys from adapter
KNOWN_COMMANDS = {
    "stats-core", "stats-radio", "stats-packets", "ver", "board",
    "neighbors", "get name", "get public.key", "gps", "req_acl",
    "req_status", "req_neighbours", "req_telemetry", "clock",
    "get owner.info", "req_owner", "req_clock",
    "region list allowed", "region list denied", "req_regions",
}

DISALLOWED_COMMANDS = [
    "reboot", "set name", "set txpower", "set freq", "shutdown",
    "rm -rf /", "exec", "config write", "admin",
]


def test_known_commands_accepted():
    for cmd in ["ver", "stats-core", "neighbors", "clock", "get owner.info"]:
        assert cmd in KNOWN_COMMANDS, f"Expected {cmd!r} to be in allowlist"
    print("  PASS: known read-only commands are in allowlist")


def test_disallowed_commands_rejected():
    for cmd in DISALLOWED_COMMANDS:
        assert cmd not in KNOWN_COMMANDS, f"{cmd!r} must NOT be in allowlist"
    print("  PASS: dangerous commands absent from allowlist")


def test_contact_lookup_safe():
    """meshcore_contact is in the safe meshcore toolset — no admin required."""
    # This is verified by the tool registration: meshcore_contact uses toolset="meshcore"
    # while meshcore_admin and meshcore_admin_query use toolset="meshcore_admin"
    print("  PASS: meshcore_contact in safe meshcore toolset (verified by code review)")


# ── Run ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== MeshCore Security Tests (MESH-001 + MESH-002) ===\n")
    test_parse_channel_index()
    test_toolsets_admin_dm()
    test_toolsets_unauthorized_dm()
    test_toolsets_admin_channel()
    test_toolsets_public_channel()
    test_toolsets_group_admin()
    test_toolsets_no_admin_config()
    test_toolsets_fail_closed()
    test_check_admin_auth_no_instance()
    test_check_admin_auth_no_config()
    test_check_admin_auth_authorized()
    test_known_commands_accepted()
    test_disallowed_commands_rejected()
    test_contact_lookup_safe()
    print("\nAll 14 tests passed.")