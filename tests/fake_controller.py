"""A fake EasyTouch behind an RS-485-to-TCP bridge, for tests.

It broadcasts status frames on a timer like the real controller (every ~2 s;
much faster here), answers schedule reads, acknowledges commands, and can be
told to misbehave: drop writes silently, go quiet while keeping the socket
open, or start a 5-minute (scaled) high-speed start timer like the real one.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from custom_components.easytouch_local.protocol import (
    Action,
    ChlorPacket,
    FrameParser,
    Packet,
)

# Real broadcast captured from the EasyTouch2 4P (pump running, Pool on).
REAL_STATUS = bytes.fromhex(
    "0d 08 20 00 00 00 00 00 00 00 00 00 08 04 f4 f4 00 00 5b 00 00 00 00 00 00 96 54 03 0d"
)


@dataclass
class FakeState:
    hour: int = 13
    minute: int = 8
    circuits: set[int] = field(default_factory=set)
    mode: int = 0
    delay: int = 0
    water: int = 0xF4  # faulted sensor, like the real one
    air: int = 91
    # slot -> 7-byte payload; slots 7 and 8 hold egg timers like a real panel
    # circuit -> (function, name id); 19 = not used. 201 = custom name slot 1.
    circuit_config: dict[int, tuple[int, int]] = field(
        default_factory=lambda: {
            2: (0, 43),  # generic, "High Speed"
            3: (7, 201),  # light, custom name 1
            4: (19, 0),  # not used
            6: (2 | 64, 61),  # pool (freeze protect), "Pool"
        }
    )
    custom_names: dict[int, str] = field(default_factory=lambda: {1: "Waterfall"})
    chlor_pool: int = 50
    chlor_spa: int = 10
    super_hours: int = 0
    schedules: dict[int, bytes] = field(
        default_factory=lambda: {
            7: bytes([7, 2, 25, 0, 12, 0, 0]),  # egg timer, circuit 2, 12 h
            8: bytes([8, 6, 25, 0, 12, 0, 0]),  # egg timer, circuit 6, 12 h
        }
    )


class FakeEasyTouch:
    def __init__(self, interval: float = 0.05) -> None:
        self.interval = interval
        self.state = FakeState()
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self.clients: list[asyncio.StreamWriter] = []
        self.received: list[Packet] = []
        self.wrong_header: list[Packet] = []
        self.silent = False  # stop broadcasting but keep sockets open
        self.drop_writes: dict[int, int] = {}  # action -> number of writes to ignore
        self.ack = True
        self.reject_connections = False
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ #
    async def start(self, serve: bool = True) -> None:
        if serve:
            self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
        self._tasks.append(asyncio.get_running_loop().create_task(self._broadcast_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        await self.kick_clients()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def kick_clients(self) -> None:
        for w in list(self.clients):
            w.close()
            with contextlib.suppress(Exception):
                await w.wait_closed()
        self.clients.clear()

    async def dial(self, host: str, port: int, retry: float = 0.05) -> None:
        """Act like an EW11 in TCP client mode: connect out, reconnect when dropped."""

        async def loop() -> None:
            while True:
                try:
                    reader, writer = await asyncio.open_connection(host, port)
                except OSError:
                    await asyncio.sleep(retry)
                    continue
                await self._client(reader, writer)
                await asyncio.sleep(retry)

        self._tasks.append(asyncio.get_running_loop().create_task(loop()))

    def commands(self, action: int) -> list[Packet]:
        return [p for p in self.received if p.action == action]

    # ------------------------------------------------------------------ #
    def status_payload(self) -> bytes:
        s = self.state
        p = bytearray(29)
        p[0], p[1] = s.hour, s.minute
        for c in s.circuits:
            idx, bit = divmod(c - 1, 8)
            if idx < 5:
                p[2 + idx] |= 1 << bit
        p[9] = s.mode
        p[12] = s.delay
        p[13] = 4
        p[14] = p[15] = s.water
        p[18] = s.air
        p[25], p[26], p[27], p[28] = 0x96, 0x54, 0x03, 0x0D
        return bytes(p)

    def _frame(self, action: int, payload: bytes, dest: int = 15) -> bytes:
        return Packet(1, dest, 16, action, payload).to_bytes()

    async def _write_all(self, data: bytes) -> None:
        for w in list(self.clients):
            try:
                w.write(data)
                await w.drain()
            except (ConnectionError, OSError):
                with contextlib.suppress(ValueError):
                    self.clients.remove(w)

    async def _broadcast_loop(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(self.interval)
            if self.silent:
                continue
            tick += 1
            data = b"\x00\x11" + self._frame(Action.STATUS, self.status_payload())  # a little noise
            if tick % 5 == 0:
                data += ChlorPacket(0x50, 0x11, bytes([30])).to_bytes()
                data += ChlorPacket(0x00, 0x12, bytes([56, 0x02])).to_bytes()
            await self._write_all(data)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.reject_connections:
            writer.close()
            return
        self.clients.append(writer)
        parser = FrameParser()
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                for frame in parser.feed(data):
                    if isinstance(frame, Packet) and frame.dest == 16:
                        if frame.sub != 1:
                            # The real panel ignores a header byte that doesn't
                            # match its own broadcasts.
                            self.wrong_header.append(frame)
                            continue
                        self.received.append(frame)
                        await self._handle(frame)
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(ValueError):
                self.clients.remove(writer)

    async def _handle(self, pkt: Packet) -> None:
        s = self.state
        if self.silent:
            return  # a dead link: nothing gets through either way
        if self.drop_writes.get(pkt.action, 0) > 0:
            self.drop_writes[pkt.action] -= 1
            return  # silently ignored, like the real panel sometimes does
        reply = b""
        if pkt.action == Action.SET_CIRCUIT:
            circuit, on = pkt.payload[0], pkt.payload[1]
            if on:
                s.circuits.add(circuit)
            else:
                s.circuits.discard(circuit)
        elif pkt.action == Action.SET_SCHEDULE:
            slot = pkt.payload[0]
            if pkt.payload[1] == 0:
                s.schedules.pop(slot, None)
            else:
                s.schedules[slot] = bytes(pkt.payload[:7])
        elif pkt.action == Action.SET_CHLOR:
            s.chlor_spa = pkt.payload[0] >> 1
            s.chlor_pool = pkt.payload[1]
            s.super_hours = pkt.payload[2] - 128 if pkt.payload[2] & 0x80 else 0
        elif pkt.action == Action.GET_CHLOR:
            name = b"Intellichlor--40"
            body = bytes([(s.chlor_spa << 1) | 1, s.chlor_pool, 0, 56, 0x80, s.super_hours]) + name
            reply = self._frame(Action.CHLOR_STATUS, body)
        elif pkt.action == Action.SET_CLOCK:
            s.hour, s.minute = pkt.payload[0], pkt.payload[1]
        elif pkt.action == Action.GET_CIRCUIT:
            cid = pkt.payload[0]
            func, name = s.circuit_config.get(cid, (19, 0))
            reply = self._frame(Action.CIRCUIT_CONFIG, bytes([cid, func, name, 0, 0]))
        elif pkt.action == Action.GET_CUSTOM_NAME:
            slot = pkt.payload[0]
            text = s.custom_names.get(slot, "").encode()[:11].ljust(11, b"\x00")
            reply = self._frame(Action.CUSTOM_NAME, bytes([slot]) + text)
        elif pkt.action == Action.GET_SCHEDULE:
            slot = pkt.payload[0]
            body = s.schedules.get(slot, bytes([slot, 0, 0, 0, 0, 0, 0]))
            reply = self._frame(Action.SCHEDULE_CONFIG, body)
        if self.ack and pkt.action not in (
            Action.GET_SCHEDULE,
            Action.GET_CIRCUIT,
            Action.GET_CUSTOM_NAME,
            Action.GET_CHLOR,
        ):
            reply = Packet(1, pkt.source, 16, Action.ACK, bytes([pkt.action])).to_bytes() + reply
        if reply:
            await self._write_all(reply)
