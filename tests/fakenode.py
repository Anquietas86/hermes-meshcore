"""A scriptable fake MeshCore companion node speaking the TCP protocol."""

import asyncio
import os
import struct
from typing import List, Optional


def frame(payload: bytes) -> bytes:
    return b"\x3e" + len(payload).to_bytes(2, "little") + payload


def self_info_body(name: str = "HermesBot", pubkey: bytes = bytes.fromhex("aa" * 32)) -> bytes:
    body = bytes([1, 22, 22]) + pubkey + struct.pack("<ii", -34928000, 138600000)
    body += bytes([0, 0, 0, 0]) + struct.pack("<II", 915800, 250000) + bytes([11, 5])
    return body + name.encode()


def contact_body(pubkey_hex: str, name: str, ctype: int = 1, hops: int = 2, path: bytes = b"\x11\x22") -> bytes:
    plen = 0xFF if hops < 0 else hops
    body = bytes.fromhex(pubkey_hex) + bytes([ctype, 0, plen]) + path.ljust(64, b"\0")
    body += name.encode().ljust(32, b"\0") + struct.pack("<IiiI", 1700000000, 0, 0, 1700000000)
    return body


def contact_msg_body(prefix_hex: str, text: str, ts: int = 1700000100, txt_type: int = 0, v3: bool = True) -> bytes:
    head = (b"\x10\x00\x00" if v3 else b"")
    return head + bytes.fromhex(prefix_hex) + bytes([1, txt_type]) + struct.pack("<I", ts) + text.encode()


def channel_msg_body(idx: int, text: str, ts: int = 1700000200, v3: bool = True) -> bytes:
    head = (b"\x10\x00\x00" if v3 else b"")
    return head + bytes([idx, 0, 0]) + struct.pack("<I", ts) + text.encode()


def status_data(battery=4012, uptime=90061) -> bytes:
    return struct.pack("<HHhhIIIIIIIIHhHHI", battery, 0, -110, -80, 10, 20, 30, uptime,
                       1, 2, 3, 4, 0, 24, 0, 0, 40) + struct.pack("<I", 7)


class FakeNode:
    ALICE = "a1" * 32
    BOB = "b2" * 32
    RPT = "c3" * 32
    FLOOD_RPT = "d4" * 32

    def __init__(self) -> None:
        self.server: Optional[asyncio.base_events.Server] = None
        self.port = 0
        self.writers: List[asyncio.StreamWriter] = []
        self.received: List[bytes] = []
        self.inbox: List[tuple] = []  # (code, body)
        self.silent = False
        self.login_ok = True
        self.repeater_replies = True
        self.on_binary_req = None  # hook(sub_type) -> optional side effect
        self.connections = 0
        self.contacts = [
            contact_body(self.ALICE, "Alice", 1, 1),
            contact_body(self.BOB, "Bob", 1, 3),
            contact_body(self.RPT, "Hilltop RPT", 2, 2, b"\x11\x22"),
            contact_body(self.FLOOD_RPT, "Valley RPT", 2, -1),
        ]

    async def start(self) -> "FakeNode":
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for w in self.writers:
            w.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def drop_connections(self) -> None:
        for w in self.writers:
            w.close()
        self.writers.clear()

    async def push(self, code: int, body: bytes = b"") -> None:
        for w in list(self.writers):
            w.write(frame(bytes([code]) + body))
            await w.drain()

    async def deliver(self, code: int, body: bytes) -> None:
        """Queue a message and tell the host it is waiting."""
        self.inbox.append((code, body))
        await self.push(0x83)

    def sent(self, code: int) -> List[bytes]:
        return [r for r in self.received if r and r[0] == code]

    async def _client(self, reader, writer) -> None:
        self.connections += 1
        self.writers.append(writer)
        buf = b""
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                buf += data
                while len(buf) >= 3:
                    assert buf[0] == 0x3C, buf[:8]
                    size = int.from_bytes(buf[1:3], "little")
                    if len(buf) < 3 + size:
                        break
                    payload, buf = buf[3:3 + size], buf[3 + size:]
                    self.received.append(payload)
                    if not self.silent:
                        await self._answer(payload, writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if writer in self.writers:
                self.writers.remove(writer)
            writer.close()

    async def _answer(self, payload: bytes, w) -> None:
        code, body = payload[0], payload[1:]
        out: List[bytes] = []
        later: List[bytes] = []
        if code == 0x01:
            out.append(b"\x05" + self_info_body())
        elif code == 0x16:
            out.append(b"\x0d" + bytes([10, 50, 8]) + bytes(4) + b"build".ljust(12, b"\0")
                       + b"Heltec V3".ljust(40, b"\0") + b"v1.9.0".ljust(20, b"\0") + b"\x00\x00")
        elif code == 0x04:
            out.append(b"\x02" + len(self.contacts).to_bytes(4, "little"))
            out += [b"\x03" + c for c in self.contacts]
            out.append(b"\x04" + bytes(4))
        elif code == 0x1F:
            idx = body[0]
            names = {0: "Public", 1: "Hermes"}
            out.append(b"\x12" + bytes([idx]) + names[idx].encode().ljust(32, b"\0") + bytes(16)
                       if idx in names else b"\x01\x02")
        elif code in (0x07,):
            out.append(b"\x00")
        elif code == 0x0A:
            if self.inbox:
                c, b = self.inbox.pop(0)
                out.append(bytes([c]) + b)
            else:
                out.append(b"\x0a")
        elif code == 0x14:
            out.append(b"\x0c" + struct.pack("<HII", 4100, 10, 100))
        elif code == 0x38:
            kind = body[0]
            data = {0: struct.pack("<HIHB", 4100, 3600, 0, 0), 1: struct.pack("<hbbII", -112, -70, 28, 5, 6),
                    2: struct.pack("<6I", 1, 2, 3, 4, 5, 6)}[kind]
            out.append(b"\x18" + bytes([kind]) + data)
        elif code == 0x02:
            out.append(b"\x06\x00" + os.urandom(4) + struct.pack("<I", 3000))
        elif code == 0x03:
            out.append(b"\x00")
        elif code == 0x1A:
            out.append(b"\x06\x00" + os.urandom(4) + struct.pack("<I", 2000))
            prefix = body[:6]
            later.append((b"\x85\x00" if self.login_ok else b"\x86\x00") + prefix)
        elif code in (0x32, 0x39):
            tag = os.urandom(4)
            out.append(b"\x06\x00" + tag + struct.pack("<I", 2000))
            sub = body[32]
            if self.on_binary_req:
                await self.on_binary_req(code, sub)
            if self.repeater_replies:
                if code == 0x32 and sub == 0x01:
                    data = status_data()
                elif code == 0x32 and sub == 0x06:
                    data = struct.pack("<hh", 2, 1) + bytes.fromhex("11223344") + struct.pack("<ib", 120, 40)
                elif code == 0x32 and sub == 0x03:
                    data = bytes([1, 0x74]) + struct.pack(">H", 412)
                elif code == 0x39 and sub == 0x02:
                    data = tag + b"Hilltop RPT\nVK5ABC"
                elif code == 0x39 and sub == 0x03:
                    data = tag + struct.pack("<I", 1700001234)
                else:
                    data = tag
                later.append(b"\x8c\x00" + tag + data)
        for item in out:
            w.write(frame(item))
        await w.drain()
        if later:
            await asyncio.sleep(0.05)
            for item in later:
                w.write(frame(item))
            await w.drain()
