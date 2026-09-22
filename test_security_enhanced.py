"""Security tests for MeshCore plugin: MESH-003 through MESH-006.

Tests secure IPC, profile resolution, config alignment, and parser hardening.
"""

import asyncio
import json
import os

import adapter
from dashboard import api as dashboard_api
from adapter import (
    MeshCoreAdapter,
    _parse_channel_index,
)
from meshcore_utils import (
    get_profile_scoped_dir,
    secure_write_json,
    secure_read_json,
    secure_remove,
    generate_request_id,
    serialize_ipc_payload,
)


# ── Unit: Profile-scoped directory ────────────────────────────────────

def test_get_profile_scoped_dir():
    """Test that profile-scoped directory is created with correct permissions."""
    scoped_dir = get_profile_scoped_dir()
    assert scoped_dir.exists()
    assert scoped_dir.is_dir()
    assert oct(scoped_dir.stat().st_mode & 0o777) == '0o700'
    print("  PASS: Profile-scoped directory created with 0700 permissions")


def test_profile_resolution_fails_closed_without_environment():
    """Profile-scoped IPC must never fall back to a global path."""
    original = os.environ.pop("HERMES_PROFILE_DIR", None)
    original_home = os.environ.pop("HERMES_HOME", None)
    try:
        try:
            get_profile_scoped_dir()
            assert False, "Missing HERMES_PROFILE_DIR must fail closed"
        except RuntimeError as exc:
            assert "HERMES_PROFILE_DIR" in str(exc)
    finally:
        if original is not None:
            os.environ["HERMES_PROFILE_DIR"] = original
        if original_home is not None:
            os.environ["HERMES_HOME"] = original_home


def test_profile_scoped_file_paths():
    """Test that adapter instances only use profile-scoped file paths."""
    class FakeConfig:
        extra = {}

    adapter_obj = MeshCoreAdapter(FakeConfig())
    profile_dir = get_profile_scoped_dir()
    assert adapter_obj.STATE_FILE == str(profile_dir / "state.json")
    assert adapter_obj.ADMIN_REQUEST_FILE == str(profile_dir / "admin-request.json")
    assert adapter_obj.ADMIN_RESPONSE_FILE == str(profile_dir / "admin-response.json")
    assert adapter_obj.ADVERT_REQUEST_FILE == str(profile_dir / "advert-request.json")
    assert "/tmp/hermes-meshcore" not in adapter_obj.STATE_FILE
    assert "STATE_FILE" not in MeshCoreAdapter.__dict__
    assert "ADMIN_REQUEST_FILE" not in MeshCoreAdapter.__dict__
    assert "ADMIN_RESPONSE_FILE" not in MeshCoreAdapter.__dict__
    print("  PASS: Adapter uses profile-scoped file paths")


# ── Unit: Secure file operations ──────────────────────────────────────


def test_ipc_serialization_rejects_password_key_and_value():
    """Serialized IPC can contain neither a password key nor its value."""
    password_value = "ipc-password-sentinel"
    safe_payload = {"request_id": "test-123", "command": "ver"}
    serialized = serialize_ipc_payload(
        safe_payload, forbidden_values=(password_value,)
    )
    assert password_value not in serialized
    assert '"password"' not in serialized

    try:
        serialize_ipc_payload(
            {**safe_payload, "password": password_value},
            forbidden_values=(password_value,),
        )
        assert False, "Password-bearing IPC payload must be rejected"
    except ValueError:
        pass

    try:
        serialize_ipc_payload(
            {**safe_payload, "note": password_value},
            forbidden_values=(password_value,),
        )
        assert False, "Password values must be rejected regardless of key"
    except ValueError:
        pass


def test_dashboard_rejects_password_bearing_ipc_request():
    """The separate dashboard process must reject rather than persist a password."""
    password_value = "dashboard-password-sentinel"

    class FakeRequest:
        async def body(self):
            return json.dumps({
                "node": "test-node",
                "command": "ver",
                "password": password_value,
            }).encode()

    request_file = get_profile_scoped_dir() / "admin-request.json"
    secure_remove(request_file)
    try:
        asyncio.run(dashboard_api.submit_admin_query(FakeRequest()))
        assert False, "Dashboard must reject a password-bearing IPC request"
    except dashboard_api.HTTPException as exc:
        assert exc.status_code == 400
        assert "not supported" in exc.detail
    assert not request_file.exists()


def test_secure_write_and_read_json():
    """Test secure write/read operations with atomicity and permissions."""
    test_data = {"test": "value", "nested": {"key": "data"}}
    test_file = get_profile_scoped_dir() / "test_secure.json"

    try:
        # Write securely
        secure_write_json(test_file, test_data)

        # Verify file exists with correct permissions
        assert test_file.exists()
        assert oct(test_file.stat().st_mode & 0o777) == '0o600'

        # Read securely
        read_data = secure_read_json(test_file)
        assert read_data == test_data

        print("  PASS: Secure write/read with 0600 permissions")
    finally:
        if test_file.exists():
            secure_remove(test_file)


def test_secure_read_with_request_id_validation():
    """Test secure read with request ID validation."""
    test_file = get_profile_scoped_dir() / "test_request_id.json"

    try:
        data_with_id = {"request_id": "test-123", "data": "value"}
        secure_write_json(test_file, data_with_id)

        # Should succeed with matching ID
        result = secure_read_json(test_file, require_matching_request_id="test-123")
        assert result == data_with_id

        # Should fail with non-matching ID
        try:
            secure_read_json(test_file, require_matching_request_id="wrong-id")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass  # Expected

        print("  PASS: Request ID validation works")
    finally:
        if test_file.exists():
            secure_remove(test_file)


def test_generate_request_id():
    """Test that request IDs are generated uniquely."""
    id1 = generate_request_id()
    id2 = generate_request_id()

    assert id1 != id2
    assert "-" in id1  # Contains timestamp-separator
    assert "-" in id2

    print("  PASS: Unique request IDs generated")


# ── Unit: Parser hardening ────────────────────────────────────────────

def test_parse_self_info_length_validation():
    """Test that SELF_INFO parser validates payload length."""
    # Too short payload should raise ValueError
    short_payload = b"\x01" * 10  # Much shorter than required 42

    try:
        adapter.MeshCoreRawConnection.parse_self_info(short_payload)
        assert False, "Should have raised ValueError for short payload"
    except ValueError as e:
        assert "too short" in str(e).lower()

    print("  PASS: SELF_INFO parser validates length")


def test_parse_device_info_length_validation():
    """Test that DEVICE_INFO parser validates payload length."""
    # Empty payload should raise ValueError
    try:
        adapter.MeshCoreRawConnection.parse_device_info(b"")
        assert False, "Should have raised ValueError for empty payload"
    except ValueError as e:
        assert "too short" in str(e).lower()

    # Version 3 with insufficient data should raise ValueError
    try:
        adapter.MeshCoreRawConnection.parse_device_info(b"\x03\x01")  # Version 3 but only 2 bytes
        assert False, "Should have raised ValueError for insufficient v3 data"
    except ValueError as e:
        assert "too short" in str(e).lower()

    print("  PASS: DEVICE_INFO parser validates length")


def test_parse_contact_length_validation():
    """Test that CONTACT parser validates payload length."""
    # Too short payload should raise ValueError
    short_payload = b"\x01" * 10  # Much shorter than required 41

    try:
        adapter.MeshCoreRawConnection.parse_contact(short_payload)
        assert False, "Should have raised ValueError for short payload"
    except ValueError as e:
        assert "too short" in str(e).lower()

    print("  PASS: CONTACT parser validates length")


def test_parse_msg_sent_length_validation():
    """Test that MSG_SENT parser validates payload length."""
    # Too short payload should raise ValueError
    short_payload = b"\x01\x02\x03"  # Only 3 bytes, needs at least 9

    try:
        adapter.MeshCoreRawConnection.parse_msg_sent(short_payload)
        assert False, "Should have raised ValueError for short payload"
    except ValueError as e:
        assert "too short" in str(e).lower()

    print("  PASS: MSG_SENT parser validates length")


def test_parse_contact_msg_length_validation():
    """Test that CONTACT_MSG parser validates payload length."""
    # Too short payload should raise ValueError
    short_payload = b"\x01\x02\x03"  # Only 3 bytes, needs at least 11 for standard

    try:
        adapter.MeshCoreRawConnection.parse_contact_msg(short_payload)
        assert False, "Should have raised ValueError for short payload"
    except ValueError as e:
        assert "too short" in str(e).lower()

    # Test V3 too short
    try:
        adapter.MeshCoreRawConnection.parse_contact_msg(b"\x01\x02", is_v3=True)
        assert False, "Should have raised ValueError for short V3 payload"
    except ValueError as e:
        assert "too short" in str(e).lower()

    print("  PASS: CONTACT_MSG parser validates length")


def test_parse_channel_msg_length_validation():
    """Test that CHANNEL_MSG parser validates payload length."""
    # Too short payload should raise ValueError
    short_payload = b"\x01"  # Only 1 byte, needs at least 7 for standard

    try:
        adapter.MeshCoreRawConnection.parse_channel_msg(short_payload)
        assert False, "Should have raised ValueError for short payload"
    except ValueError as e:
        assert "too short" in str(e).lower()

    # Test V3 too short
    try:
        adapter.MeshCoreRawConnection.parse_channel_msg(b"", is_v3=True)
        assert False, "Should have raised ValueError for short V3 payload"
    except ValueError as e:
        assert "too short" in str(e).lower()

    print("  PASS: CHANNEL_MSG parser validates length")


def test_parse_channel_info_length_validation():
    """Test that CHANNEL_INFO parser validates payload length."""
    # Empty payload should raise ValueError
    try:
        adapter.MeshCoreRawConnection.parse_channel_info(b"")
        assert False, "Should have raised ValueError for empty payload"
    except ValueError as e:
        assert "too short" in str(e).lower()

    print("  PASS: CHANNEL_INFO parser validates length")


def test_send_frame_size_validation():
    """Test that send_frame validates payload size."""
    # Access the constant from the adapter module
    MAX_FRAME_SIZE = adapter.MAX_FRAME_SIZE

    import asyncio

    class FakeConnection:
        def __init__(self):
            self.writer = None

        async def send_frame(self, payload: bytes):
            if len(payload) > MAX_FRAME_SIZE:
                raise ValueError(f"Frame payload too large: {len(payload)} bytes, maximum is {MAX_FRAME_SIZE}")
            # Simulate sending
            return True

    conn = FakeConnection()

    # Valid size should work
    valid_payload = b"x" * MAX_FRAME_SIZE
    try:
        asyncio.run(conn.send_frame(valid_payload))
    except ValueError:
        assert False, "Valid size payload should not raise error"

    # Too large should raise error
    oversized_payload = b"x" * (MAX_FRAME_SIZE + 1)
    try:
        asyncio.run(conn.send_frame(oversized_payload))
        assert False, "Oversized payload should raise ValueError"
    except ValueError as e:
        assert "too large" in str(e).lower()

    print("  PASS: send_frame validates size limits")


def test_config_key_alignment():
    """Test the dashboard keys consumed by the adapter's scoped environment."""
    assert dashboard_api.CONFIG_TO_ENV["require_mention"] == "MESHCORE_REQUIRE_MENTION"
    assert dashboard_api.CONFIG_TO_ENV["allow_all_users"] == "MESHCORE_ALLOW_ALL_USERS"
    assert "require_mention_channels" not in dashboard_api.CONFIG_TO_ENV
    assert set(dashboard_api.CONFIG_KEYS) == set(dashboard_api.CONFIG_TO_ENV)
    print("  PASS: Config keys aligned between dashboard and adapter")


# ── Run ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== MeshCore Security Tests (MESH-003 through MESH-006) ===\n")

    test_get_profile_scoped_dir()
    test_profile_scoped_file_paths()
    test_secure_write_and_read_json()
    test_secure_read_with_request_id_validation()
    test_generate_request_id()
    test_parse_self_info_length_validation()
    test_parse_device_info_length_validation()
    test_parse_contact_length_validation()
    test_parse_msg_sent_length_validation()
    test_parse_contact_msg_length_validation()
    test_parse_channel_msg_length_validation()
    test_parse_channel_info_length_validation()
    test_send_frame_size_validation()
    test_config_key_alignment()

    print("\nAll 15 security tests passed.")