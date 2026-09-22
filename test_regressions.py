"""Boundary and cross-session regressions exercising production code."""

import asyncio
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import time
from unittest.mock import AsyncMock, patch

import adapter
from adapter import MeshCoreAdapter, MeshCoreRawConnection as Raw
from meshcore_utils import secure_read_json, secure_write_json


def rejects(function, payload, **kwargs):
    try:
        function(payload, **kwargs)
    except ValueError:
        return
    raise AssertionError(f"Accepted truncated payload of length {len(payload)}")


def test_fixed_parser_boundaries():
    for parser, minimum in ((Raw.parse_self_info, 57), (Raw.parse_contact, 147),
                            (Raw.parse_channel_info, 49), (Raw.parse_msg_sent, 9)):
        for length in range(minimum):
            rejects(parser, bytes(length))
        assert parser(bytes(minimum))
    for version in (0, 2):
        assert Raw.parse_device_info(bytes([version])) == {"fw_ver": version}
    rejects(Raw.parse_device_info, b"")
    for version in (3, 9, 10):
        for length in range(1, 79):
            rejects(Raw.parse_device_info, bytes([version]) + bytes(length - 1))
        assert Raw.parse_device_info(bytes([version]) + bytes(78))["fw_ver"] == version
    extended = Raw.parse_device_info(b"\x0a" + bytes(78) + b"\x01\x02")
    assert extended["repeat"] is True and extended["path_hash_mode"] == 2
    # Fixed-width name padding and the secret must never become part of the name.
    channel = Raw.parse_channel_info(b"\x02" + b"test\0".ljust(32, b"x") + bytes(16))
    assert channel == {"channel_idx": 2, "channel_name": "test"}
    assert Raw.parse_channel_info(b"\x00" + b"a" * 32 + bytes(16))["channel_name"] == "a" * 32


def test_message_boundaries_and_signatures():
    for v3 in (False, True):
        prefix = b"\xfc\0\0" if v3 else b""
        for parser, minimum in ((Raw.parse_contact_msg, 15 if v3 else 12),
                                (Raw.parse_channel_msg, 10 if v3 else 7)):
            for length in range(minimum):
                rejects(parser, bytes(length), is_v3=v3)
            assert parser(bytes(minimum), is_v3=v3)["text"] == ""
        for hop in (0, 3, 255):
            stamp = struct.pack("<I", 123456)
            contact = prefix + b"abcdef" + bytes([hop, 0]) + stamp
            channel = prefix + bytes([2, hop, 0]) + stamp
            for parser, payload in ((Raw.parse_contact_msg, contact), (Raw.parse_channel_msg, channel)):
                parsed = parser(payload + b"sender: hello", is_v3=v3)
                assert parsed["sender_timestamp"] == 123456
                assert parsed["text"] == "sender: hello" and parsed["path_len"] == hop
            signed = prefix + b"abcdef" + bytes([hop, 2]) + stamp
            for size in range(4):
                rejects(Raw.parse_contact_msg, signed + bytes(size), is_v3=v3)
            assert Raw.parse_contact_msg(signed + b"abcd", is_v3=v3)["signature"] == "61626364"


def test_battery_stats_and_dashboard():
    for length in (0, 1, 3, 4, 5, 6, 7, 8, 9):
        rejects(Raw.parse_battery, bytes(length))
    assert Raw.parse_battery(b"\x10\x0e")["level"] == 3600
    assert Raw.parse_battery(bytes(10))["total_kb"] == 0
    rejects(Raw.parse_stats, b"")
    for kind, size in ((0, 9), (1, 12), (2, 24)):
        for length in range(size):
            rejects(Raw.parse_stats, bytes([kind]) + bytes(length))
        assert Raw.parse_stats(bytes([kind]) + bytes(size))
    for length in (25, 26, 27):
        rejects(Raw.parse_stats, b"\x02" + bytes(length))
    assert Raw.parse_stats(b"\x02" + bytes(28))["recv_errors"] == 0
    obj = object.__new__(MeshCoreAdapter)
    obj._stats_cache = {"radio": Raw.parse_stats(b"\x01" + struct.pack("<hbbII", -110, -75, 13, 2, 3))}
    result = obj._build_stats_info()
    assert (result["noise"], result["rssi"], result["snr"]) == (-110, -75, 3.25)


def test_remote_response_boundaries():
    for length in range(60):
        rejects(lambda p: MeshCoreAdapter._parse_status_response(p, "node"), bytes(length))
    for length in (60, 64):
        assert "Status" in MeshCoreAdapter._parse_status_response(bytes(length), "node")
    for length in (61, 62, 63):
        rejects(lambda p: MeshCoreAdapter._parse_status_response(p, "node"), bytes(length))
    for length in range(5):
        rejects(lambda p: MeshCoreAdapter._parse_binary_response(p, "node"), bytes(length))
    for length in (57, 61):
        assert "Status" in MeshCoreAdapter._parse_binary_response(bytes(length), "node")
    for length in (58, 59, 60):
        rejects(lambda p: MeshCoreAdapter._parse_binary_response(p, "node"), bytes(length))
    parser = lambda p: MeshCoreAdapter._parse_neighbours_response(p, "node")
    for length in range(4):
        rejects(parser, bytes(length))
    for length in range(9):
        rejects(parser, struct.pack("<HH", 1, 1) + bytes(length))
    assert "In response: 1" in parser(struct.pack("<HH", 1, 1) + bytes(9))
    assert "In response: 0" in parser(bytes(4))
    rejects(parser, struct.pack("<HH", 0, 1) + bytes(9))


def test_real_frame_reader_boundaries():
    async def read(data):
        conn = Raw("unused", 0)
        conn.reader = asyncio.StreamReader()
        conn.reader.feed_data(data)
        conn.reader.feed_eof()
        return await conn.read_frame()

    for size in (1, adapter.MAX_FRAME_SIZE):
        assert asyncio.run(read(b">" + struct.pack("<H", size) + bytes(size))) == (0, bytes(size - 1))
    for size in (0, adapter.MAX_FRAME_SIZE + 1):
        rejects(lambda p: asyncio.run(read(p)), b">" + struct.pack("<H", size))
    for data in (b"", b">", b">\x01", b">\x02\x00\x01"):
        try:
            asyncio.run(read(data))
        except ConnectionError:
            pass
        else:
            raise AssertionError("Accepted truncated frame")
    conn = Raw("unused", 0)
    for data in (b"", bytes(adapter.MAX_FRAME_SIZE + 1)):
        rejects(lambda p: asyncio.run(conn.send_frame(p)), data)


def test_contact_list_header_boundary():
    async def exercise():
        obj = object.__new__(MeshCoreAdapter)
        obj._conn = AsyncMock()
        for length in range(4):
            obj._conn.send_command.return_value = (adapter.PKT_CONTACT_START, bytes(length))
            try:
                await obj._load_contacts()
            except ValueError:
                pass
            else:
                raise AssertionError("Accepted truncated CONTACT_START")
        obj._conn.read_frame.assert_not_awaited()
        obj._conn.send_command.return_value = (adapter.PKT_CONTACT_START, bytes(4))
        obj._conn.read_frame.return_value = (adapter.PKT_CONTACT_END, bytes(4))
        await obj._load_contacts()
        assert obj._contacts == {}
    asyncio.run(exercise())


def test_passwordless_ipc_without_local_singleton():
    async def exercise(directory):
        obj = object.__new__(MeshCoreAdapter)
        obj.ADMIN_REQUEST_FILE = str(directory / "admin-request.json")
        obj.ADMIN_RESPONSE_FILE = str(directory / "admin-response.json")
        obj.admin_nodes, obj.admin_channels = {"admin"}, set()
        obj.query_remote_repeater = AsyncMock(return_value={"success": True, "responses": ["ok"]})
        secure_write_json(directory / "state.json", {"updated_at": time.time()})

        async def gateway_tick(_):
            request = secure_read_json(obj.ADMIN_REQUEST_FILE)
            assert "password" not in request
            await obj._process_admin_request()

        with patch.object(adapter, "get_profile_scoped_dir", return_value=directory), \
                patch.object(MeshCoreAdapter, "_instance", None), \
                patch.object(adapter.asyncio, "sleep", side_effect=gateway_tick):
            result = json.loads(await adapter._handle_meshcore_admin_query("node", "ver"))
            assert result["success"] is True and result["responses"] == ["ok"]
            obj.query_remote_repeater.assert_awaited_once_with("node", "ver", password="", timeout=90.0)
            with patch.object(adapter, "secure_write_json") as write:
                result = json.loads(await adapter._handle_meshcore_admin_query("node", "ver", "sentinel"))
                assert result["success"] is False
                write.assert_not_called()
    with TemporaryDirectory() as tmp:
        asyncio.run(exercise(Path(tmp)))


def test_gateway_ipc_authorization_and_allowlist():
    async def exercise(directory):
        obj = object.__new__(MeshCoreAdapter)
        obj.ADMIN_REQUEST_FILE = str(directory / "admin-request.json")
        obj.ADMIN_RESPONSE_FILE = str(directory / "admin-response.json")
        obj.admin_nodes, obj.admin_channels = set(), set()
        request = {"node": "node", "command": "ver", "request_id": "test-request"}
        secure_write_json(obj.ADMIN_REQUEST_FILE, request)
        with patch.object(obj, "query_remote_repeater", new_callable=AsyncMock) as query:
            await obj._process_admin_request()
            query.assert_not_awaited()
        assert "not authorized" in secure_read_json(obj.ADMIN_RESPONSE_FILE)["error"]
        obj.admin_nodes = {"admin"}
        # Exercise the real command allowlist before any connection is used.
        secure_write_json(obj.ADMIN_REQUEST_FILE, {**request, "command": "reboot"})
        await obj._process_admin_request()
        assert "Unknown command" in secure_read_json(obj.ADMIN_RESPONSE_FILE)["error"]
        # A legacy/malicious request cannot bypass the receiver's password check.
        with patch.object(adapter, "secure_read_json", return_value={**request, "password": "sentinel"}), \
                patch.object(obj, "query_remote_repeater", new_callable=AsyncMock) as query:
            await obj._process_admin_request()
            query.assert_not_awaited()
        assert "not supported" in secure_read_json(obj.ADMIN_RESPONSE_FILE)["error"]
    with TemporaryDirectory() as tmp:
        asyncio.run(exercise(Path(tmp)))
