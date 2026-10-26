"""Connection to the EasyTouch through an RS-485-to-TCP bridge (Elfin EW11 etc.).

The client owns one TCP connection and keeps it healthy on its own:

* The EasyTouch broadcasts its status about every 2 seconds, so a healthy link
  is never quiet. If no valid controller frame arrives for
  ``heartbeat_timeout`` seconds the socket is treated as dead (even if the OS
  still thinks it is open), closed, and reopened with back-off. No integration
  reload is ever needed.
* Every command is confirmed against what the controller actually reports:
  circuit changes against the next status broadcasts, schedule writes by
  reading the slot back from the controller, clock writes against the time in
  the broadcasts. Unconfirmed commands are resent a few times, then raise.
* Commands go out one at a time, spaced apart, and only into a quiet moment on
  the bus, because the EasyTouch silently drops writes that arrive too close
  together.
* In read-only mode nothing is ever written to the bus.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
import logging
import socket
import time
from typing import Any

from .protocol import (
    CONTROLLER_ADDR,
    DEFAULT_SOURCE_ADDR,
    MAX_SCHEDULE_SLOTS,
    Action,
    ChlorPacket,
    ChlorState,
    FrameParser,
    Packet,
    ScheduleSlot,
    Status,
    WantedSchedule,
    build_command,
    cmd_get_schedule,
    cmd_set_circuit,
    cmd_set_clock,
    cmd_set_schedule,
    decode_chlor_frame,
    decode_chlor_status,
    decode_schedule,
    decode_status,
)

_LOGGER = logging.getLogger(__name__)


class EasyTouchError(Exception):
    """Base error."""


class NotConnectedError(EasyTouchError):
    """No live link to the controller."""


class ReadOnlyError(EasyTouchError):
    """The integration is set to listen only."""


class CommandNotConfirmedError(EasyTouchError):
    """The controller never showed the change we asked for."""


@dataclass(slots=True)
class Timing:
    """All the timing knobs in one place (tests shrink them)."""

    connect_timeout: float = 5.0
    heartbeat_timeout: float = 10.0  # no valid frame this long -> reconnect
    stale_after: float = 30.0  # no status this long -> entities unavailable
    ack_timeout: float = 1.5
    confirm_timeout: float = 7.0  # long enough for 3 status broadcasts
    schedule_read_timeout: float = 2.5
    command_gap: float = 1.0  # minimum time between commands
    bus_quiet: float = 0.05  # wait for this much silence before sending
    bus_quiet_max: float = 0.6
    attempts: int = 3
    backoff: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)


@dataclass(slots=True)
class LinkStats:
    connects: int = 0
    reconnects: int = 0
    watchdog_trips: int = 0
    frames: int = 0
    bad_checksums: int = 0
    commands_sent: int = 0
    commands_retried: int = 0
    commands_failed: int = 0
    last_error: str | None = None
    last_connected: float | None = None  # wall clock
    last_disconnected: float | None = None


@dataclass(slots=True)
class _Waiter:
    match: Callable[[Packet], bool]
    future: asyncio.Future[Packet] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class EasyTouchClient:
    """One self-healing connection to the pool controller."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        source_addr: int = DEFAULT_SOURCE_ADDR,
        read_only: bool = True,
        timing: Timing | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.source_addr = source_addr
        self.read_only = read_only
        self.timing = timing or Timing()
        self.stats = LinkStats()

        self.status: Status | None = None
        self.status_at: float | None = None  # monotonic
        self.chlor = ChlorState()
        self.schedules: dict[int, ScheduleSlot] = {}
        self.schedules_read_at: float | None = None

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._socket_open = False
        self._last_frame_at = 0.0
        self._last_rx_at = 0.0
        self._last_cmd_at = 0.0
        self._parser = FrameParser()
        self._waiters: list[_Waiter] = []
        self._listeners: list[Callable[[], None]] = []
        self._run_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._cmd_lock = asyncio.Lock()
        self._status_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._run_task is None or self._run_task.done():
            self._stopping = False
            self._run_task = asyncio.get_running_loop().create_task(
                self._run(), name=f"easytouch {self.host}:{self.port}"
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._run_task is not None:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._run_task
            self._run_task = None
        await self._close("stopped")

    async def wait_for_status(self, wait: float) -> bool:
        """Wait until a status broadcast has arrived (setup and config flow)."""
        if self.status is not None and self.available:
            return True
        self._status_event.clear()
        try:
            await asyncio.wait_for(self._status_event.wait(), wait)
        except TimeoutError:
            return False
        return True

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(callback)

        return remove

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        """Socket open and frames arriving."""
        return (
            self._socket_open
            and time.monotonic() - self._last_frame_at < self.timing.heartbeat_timeout
        )

    @property
    def available(self) -> bool:
        """Recent controller status we can trust."""
        return (
            self.status is not None
            and self.status_at is not None
            and time.monotonic() - self.status_at < self.timing.stale_after
        )

    @property
    def status_age(self) -> float | None:
        return None if self.status_at is None else time.monotonic() - self.status_at

    # ------------------------------------------------------------------ #
    # Connection loop
    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        failures = 0
        while not self._stopping:
            try:
                await self._connect()
                failures = 0
                await self._read_until_dead()
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, EasyTouchError) as err:
                self.stats.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.debug("EasyTouch link %s:%s: %s", self.host, self.port, err)
            except Exception:  # never let the loop die
                _LOGGER.exception("Unexpected error on the EasyTouch link")
                self.stats.last_error = "unexpected error (see log)"
            await self._close("link lost")
            if self._stopping:
                break
            delay = self.timing.backoff[min(failures, len(self.timing.backoff) - 1)]
            failures += 1
            self.stats.reconnects += 1
            await asyncio.sleep(delay)

    async def _connect(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timing.connect_timeout
        )
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError, AttributeError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        self._reader, self._writer = reader, writer
        self._parser = FrameParser()
        self._socket_open = True
        now = time.monotonic()
        self._last_frame_at = now  # grace period for the first frame
        self._last_rx_at = now
        self.stats.connects += 1
        self.stats.last_connected = time.time()
        _LOGGER.info("Connected to EasyTouch bridge %s:%s", self.host, self.port)
        self._notify()

    async def _read_until_dead(self) -> None:
        """Read frames until the link closes or goes quiet."""
        assert self._reader is not None
        while not self._stopping:
            remaining = self.timing.heartbeat_timeout - (time.monotonic() - self._last_frame_at)
            if remaining <= 0:
                self.stats.watchdog_trips += 1
                raise EasyTouchError(
                    f"no valid frame for {self.timing.heartbeat_timeout:.0f}s; reconnecting"
                )
            try:
                data = await asyncio.wait_for(self._reader.read(4096), remaining)
            except TimeoutError:
                continue  # loop re-checks and trips the watchdog
            if not data:
                raise EasyTouchError("bridge closed the connection")
            self._last_rx_at = time.monotonic()
            frames = self._parser.feed(data)
            self.stats.bad_checksums = self._parser.bad_checksums
            for frame in frames:
                self._handle_frame(frame)

    async def _close(self, reason: str) -> None:
        was_open = self._socket_open
        self._socket_open = False
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
        for waiter in self._waiters:
            if not waiter.future.done():
                waiter.future.set_exception(NotConnectedError(reason))
                waiter.future.exception()  # mark retrieved; awaiters still see it
        self._waiters.clear()
        if was_open:
            self.stats.last_disconnected = time.time()
            _LOGGER.info("EasyTouch bridge %s:%s disconnected (%s)", self.host, self.port, reason)
            self._notify()

    # ------------------------------------------------------------------ #
    # Incoming frames
    # ------------------------------------------------------------------ #
    def _handle_frame(self, frame: Packet | ChlorPacket) -> None:
        self.stats.frames += 1
        if isinstance(frame, ChlorPacket):
            before = (self.chlor.salt_ppm, self.chlor.status_byte, self.chlor.output_pct)
            decode_chlor_frame(frame, self.chlor)
            if before != (self.chlor.salt_ppm, self.chlor.status_byte, self.chlor.output_pct):
                self._notify()
            return
        if frame.source == self.source_addr:
            return  # our own transmission echoed back by the bridge
        if frame.source == CONTROLLER_ADDR:
            self._last_frame_at = time.monotonic()
        changed = False
        if frame.action == Action.STATUS and frame.source == CONTROLLER_ADDR:
            status = decode_status(frame.payload)
            if status is not None:
                changed = self.status is None or status.raw != self.status.raw or not self.available
                self.status = status
                self.status_at = time.monotonic()
                self._status_event.set()
        elif frame.action == Action.SCHEDULE_CONFIG and frame.source == CONTROLLER_ADDR:
            slot = decode_schedule(frame.payload)
            if slot is not None and 1 <= slot.slot <= MAX_SCHEDULE_SLOTS:
                changed = self.schedules.get(slot.slot) != slot
                self.schedules[slot.slot] = slot
        elif frame.action == Action.CHLOR_STATUS and frame.source == CONTROLLER_ADDR:
            before = (self.chlor.salt_ppm, self.chlor.status_byte)
            decode_chlor_status(frame.payload, self.chlor)
            changed = before != (self.chlor.salt_ppm, self.chlor.status_byte)
        for waiter in list(self._waiters):
            if not waiter.future.done() and waiter.match(frame):
                waiter.future.set_result(frame)
        if changed:
            self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("EasyTouch listener failed")

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #
    def _check_can_send(self) -> None:
        if self.read_only:
            raise ReadOnlyError("listen-only mode is on; nothing is sent to the controller")
        if self._writer is None or not self.connected:
            raise NotConnectedError("no live connection to the controller")

    async def _send(self, action: int, payload: bytes) -> None:
        self._check_can_send()
        # Space commands out; the EasyTouch drops writes that come too fast.
        wait = self.timing.command_gap - (time.monotonic() - self._last_cmd_at)
        if wait > 0:
            await asyncio.sleep(wait)
        # Slip into a quiet moment on the bus rather than over a broadcast.
        deadline = time.monotonic() + self.timing.bus_quiet_max
        while time.monotonic() < deadline:
            if time.monotonic() - self._last_rx_at >= self.timing.bus_quiet:
                break
            await asyncio.sleep(self.timing.bus_quiet / 2)
        self._check_can_send()
        assert self._writer is not None
        self._writer.write(build_command(action, payload, self.source_addr))
        await self._writer.drain()
        self._last_cmd_at = time.monotonic()
        self.stats.commands_sent += 1
        _LOGGER.debug("Sent action %s payload %s", action, payload.hex(" "))

    async def _send_or_drop(self, waiter: _Waiter, action: int, payload: bytes) -> None:
        try:
            await self._send(action, payload)
        except BaseException:
            with contextlib.suppress(ValueError):
                self._waiters.remove(waiter)
            waiter.future.cancel()
            raise

    def _expect(self, match: Callable[[Packet], bool]) -> _Waiter:
        waiter = _Waiter(match)
        self._waiters.append(waiter)
        return waiter

    async def _await(self, waiter: _Waiter, wait: float) -> Packet | None:
        try:
            return await asyncio.wait_for(asyncio.shield(waiter.future), wait)
        except TimeoutError:
            return None
        finally:
            with contextlib.suppress(ValueError):
                self._waiters.remove(waiter)
            if not waiter.future.done():
                waiter.future.cancel()

    def _ack_for(self, action: int) -> Callable[[Packet], bool]:
        return lambda p: (
            p.action == Action.ACK
            and p.source == CONTROLLER_ADDR
            and p.dest == self.source_addr
            and len(p.payload) >= 1
            and p.payload[0] == action
        )

    async def _wait_status(self, check: Callable[[Status], bool], wait: float) -> bool:
        """Wait for a status broadcast newer than now that passes ``check``."""
        start = time.monotonic()
        while time.monotonic() - start < wait:
            waiter = self._expect(
                lambda p: p.action == Action.STATUS and p.source == CONTROLLER_ADDR
            )
            pkt = await self._await(waiter, wait - (time.monotonic() - start))
            if pkt is None:
                return False
            status = decode_status(pkt.payload)
            if status is not None and check(status):
                return True
        return False

    # ------------------------------------------------------------------ #
    # Commands (all confirmed)
    # ------------------------------------------------------------------ #
    async def set_circuit(self, circuit: int, on: bool) -> None:
        """Turn a circuit on/off and wait until the controller reports it."""
        async with self._cmd_lock:
            if self.available and self.status and self.status.is_on(circuit) == on:
                return
            for attempt in range(1, self.timing.attempts + 1):
                if attempt > 1:
                    self.stats.commands_retried += 1
                    _LOGGER.warning(
                        "Circuit %s %s not confirmed; resending (try %s)",
                        circuit,
                        "on" if on else "off",
                        attempt,
                    )
                ack = self._expect(self._ack_for(Action.SET_CIRCUIT))
                await self._send_or_drop(ack, Action.SET_CIRCUIT, cmd_set_circuit(circuit, on))
                await self._await(ack, self.timing.ack_timeout)
                if await self._wait_status(
                    lambda s: s.is_on(circuit) == on, self.timing.confirm_timeout
                ):
                    return
            self.stats.commands_failed += 1
            raise CommandNotConfirmedError(
                f"the controller never showed circuit {circuit} {'on' if on else 'off'} "
                f"after {self.timing.attempts} tries"
            )

    async def read_schedule(self, slot: int) -> ScheduleSlot:
        """Ask the controller for one schedule slot."""
        async with self._cmd_lock:
            return await self._read_schedule(slot)

    async def _read_schedule(self, slot: int) -> ScheduleSlot:
        for _ in range(self.timing.attempts):
            waiter = self._expect(
                lambda p: (
                    p.action == Action.SCHEDULE_CONFIG
                    and p.source == CONTROLLER_ADDR
                    and len(p.payload) >= 1
                    and p.payload[0] == slot
                )
            )
            await self._send_or_drop(waiter, Action.GET_SCHEDULE, cmd_get_schedule(slot))
            pkt = await self._await(waiter, self.timing.schedule_read_timeout)
            if pkt is not None:
                decoded = decode_schedule(pkt.payload)
                if decoded is not None:
                    return decoded
        raise CommandNotConfirmedError(f"the controller didn't answer for schedule slot {slot}")

    async def read_all_schedules(self) -> dict[int, ScheduleSlot]:
        async with self._cmd_lock:
            result: dict[int, ScheduleSlot] = {}
            for slot in range(1, MAX_SCHEDULE_SLOTS + 1):
                result[slot] = await self._read_schedule(slot)
            self.schedules = dict(result)
            self.schedules_read_at = time.monotonic()
        self._notify()
        return result

    async def write_schedule(self, slot: int, wanted: WantedSchedule | None) -> ScheduleSlot:
        """Write (or clear) a slot, then read it back from the controller to prove it stuck."""
        async with self._cmd_lock:
            for attempt in range(1, self.timing.attempts + 1):
                if attempt > 1:
                    self.stats.commands_retried += 1
                    _LOGGER.warning(
                        "Schedule slot %s didn't stick; rewriting (try %s)", slot, attempt
                    )
                ack = self._expect(self._ack_for(Action.SET_SCHEDULE))
                await self._send_or_drop(ack, Action.SET_SCHEDULE, cmd_set_schedule(slot, wanted))
                await self._await(ack, self.timing.ack_timeout)
                readback = await self._read_schedule(slot)
                self.schedules[slot] = readback
                ok = readback.empty if wanted is None else readback.same_schedule(wanted)
                if ok:
                    self._notify()
                    return readback
            self.stats.commands_failed += 1
            what = "clear" if wanted is None else wanted.describe()
            raise CommandNotConfirmedError(f"schedule slot {slot} wouldn't take '{what}'")

    async def set_clock(self, now: datetime, auto_dst: bool = True) -> None:
        """Set the controller's clock and wait for the broadcasts to show it."""
        async with self._cmd_lock:
            for attempt in range(1, self.timing.attempts + 1):
                if attempt > 1:
                    self.stats.commands_retried += 1
                ack = self._expect(self._ack_for(Action.SET_CLOCK))
                await self._send_or_drop(ack, Action.SET_CLOCK, cmd_set_clock(now, auto_dst))
                await self._await(ack, self.timing.ack_timeout)
                target = now.hour * 60 + now.minute

                def matches(s: Status, target: int = target) -> bool:
                    diff = (s.hour * 60 + s.minute - target) % 1440
                    return diff <= 2  # same minute or the next couple

                if await self._wait_status(matches, self.timing.confirm_timeout):
                    return
            self.stats.commands_failed += 1
            raise CommandNotConfirmedError("the controller clock didn't change")

    # ------------------------------------------------------------------ #
    def diagnostics(self) -> dict[str, Any]:
        s = self.status
        return {
            "connected": self.connected,
            "available": self.available,
            "read_only": self.read_only,
            "status_age_s": None if self.status_age is None else round(self.status_age, 1),
            "status_raw": s.raw.hex(" ") if s else None,
            "schedules": {k: str(v) for k, v in sorted(self.schedules.items())},
            "chlorinator": {
                "salt_ppm": self.chlor.salt_ppm,
                "status_byte": self.chlor.status_byte,
                "output_pct": self.chlor.output_pct,
            },
            "stats": {k: getattr(self.stats, k) for k in self.stats.__dataclass_fields__},
        }
