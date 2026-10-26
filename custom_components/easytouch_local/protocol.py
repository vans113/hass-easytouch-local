"""Pentair EasyTouch RS-485 protocol: framing, decoding and command building.

Pure Python with no Home Assistant imports, so it can be unit tested on its own.

Two kinds of frame share the bus:

* Controller ("broadcast") frames:
  ``FF 00 FF | A5 sub dst src action len | payload | chk_hi chk_lo``.
  The checksum is the 16-bit sum of every byte from ``A5`` through the payload.
* IntelliChlor frames: ``10 02 | dst action payload | chk | 10 03``.
  The checksum is the low byte of the sum from ``10 02`` through the payload.

Byte meanings follow nodejs-poolController's EasyTouch board, checked against
packet captures from an EasyTouch2 4P.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum

PREAMBLE = b"\xff\x00\xff"
CONTROLLER_ADDR = 16  # the EasyTouch itself
BROADCAST_ADDR = 15
DEFAULT_SOURCE_ADDR = 33  # remote/plugin slot; njsPC's default too
# Second header byte. The controller ignores commands whose byte doesn't match
# the one in its own broadcasts (EasyTouch2 sends 0x01), so the client copies it
# from status frames, as njsPC does; this is only the fallback before any arrive.
OUT_SUB_BYTE = 33
MAX_PAYLOAD = 64

# Day bits used by schedules and the clock (EasyTouch order).
DAY_BITS = {"sun": 1, "mon": 2, "tue": 4, "wed": 8, "thu": 16, "fri": 32, "sat": 64}
WEEKDAYS = DAY_BITS["mon"] | DAY_BITS["tue"] | DAY_BITS["wed"] | DAY_BITS["thu"] | DAY_BITS["fri"]
WEEKEND = DAY_BITS["sat"] | DAY_BITS["sun"]
EVERY_DAY = WEEKDAYS | WEEKEND

# Schedule slots on an EasyTouch (schedules and egg timers share them).
MAX_SCHEDULE_SLOTS = 12
EGG_TIMER_MARKER = 25  # start-hour byte of a slot that holds an egg timer

# Water/air bytes this high mean an open or unplugged thermistor.
SENSOR_FAULT_MIN = 240

# Circuit ids an EasyTouch can have: 1-10 circuits, 11-18 features.
MAX_CIRCUIT_ID = 18
CIRCUIT_FUNCTION_NOT_USED = 19
CUSTOM_NAME_BASE = 200

# Built-in circuit names, by name id (from the EasyTouch panel's list).
CIRCUIT_NAMES = [
    "Not Used",
    "Aerator",
    "Air Blower",
    "AUX 1",
    "AUX 2",
    "AUX 3",
    "AUX 4",
    "AUX 5",
    "AUX 6",
    "AUX 7",
    "AUX 8",
    "AUX 9",
    "AUX 10",
    "Backwash",
    "Back Light",
    "BBQ Light",
    "Beach Light",
    "Booster Pump",
    "Bug Light",
    "Cabana Lights",
    "Chemical Feeder",
    "Chlorinator",
    "Cleaner",
    "Color Wheel",
    "Deck Light",
    "Drain Line",
    "Drive Light",
    "Edge Pump",
    "Entry Light",
    "Fan",
    "Fiber Optic",
    "Fiber Works",
    "Fill Line",
    "Floor Cleaner",
    "Fogger",
    "Fountain",
    "Fountain 1",
    "Fountain 2",
    "Fountain 3",
    "Fountains",
    "Front Light",
    "Garden Lights",
    "Gazebo Lights",
    "High Speed",
    "Hi-Temp",
    "House Light",
    "Jets",
    "Lights",
    "Low Speed",
    "Lo-Temp",
    "Malibu Lights",
    "Mist",
    "Music",
    "Not Used",
    "Ozonator",
    "Path Lights",
    "Patio Lights",
    "Perimeter Light",
    "PG2000",
    "Pond Light",
    "Pool Pump",
    "Pool",
    "Pool High",
    "Pool Light",
    "Pool Low",
    "SAM",
    "Pool SAM 1",
    "Pool SAM 2",
    "Pool SAM 3",
    "Security Light",
    "Slide",
    "Solar",
    "Spa",
    "Spa High",
    "Spa Light",
    "Spa Low",
    "Spa SAL",
    "Spa SAM",
    "Spa Waterfall",
    "Spillway",
    "Sprinklers",
    "Stream",
    "Statue Light",
    "Swim Jets",
    "Water Feature",
    "Water Feature Light",
    "Waterfall",
    "Waterfall 1",
    "Waterfall 2",
    "Waterfall 3",
    "Whirlpool",
    "Waterfall Light",
    "Yard Light",
    "AUX EXTRA",
    "Feature 1",
    "Feature 2",
    "Feature 3",
    "Feature 4",
    "Feature 5",
    "Feature 6",
    "Feature 7",
    "Feature 8",
]


class Action(IntEnum):
    """Action bytes we send or understand."""

    ACK = 1
    STATUS = 2
    CUSTOM_NAME = 10
    CIRCUIT_CONFIG = 11
    SCHEDULE_CONFIG = 17
    CHLOR_STATUS = 25
    SET_CLOCK = 133
    SET_CIRCUIT = 134
    SET_SCHEDULE = 145
    SET_CHLOR = 153
    GET_CUSTOM_NAME = 202
    GET_CIRCUIT = 203
    GET_SCHEDULE = 209
    GET_CHLOR = 217


class PanelMode(IntEnum):
    AUTO = 0
    SERVICE = 1
    TIMEOUT = 128
    SERVICE_TIMEOUT = 129


PANEL_MODE_NAMES = {
    PanelMode.AUTO: "auto",
    PanelMode.SERVICE: "service",
    PanelMode.TIMEOUT: "timeout",
    PanelMode.SERVICE_TIMEOUT: "service_timeout",
}

CHLOR_STATUS_FLAGS = (
    (0x01, "low_flow"),
    (0x02, "low_salt"),
    (0x04, "very_low_salt"),
    (0x08, "high_current"),
    (0x10, "clean_cell"),
    (0x20, "low_voltage"),
    (0x40, "low_water_temp"),
    (0x80, "comm_lost"),
)


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Packet:
    """A controller-protocol frame."""

    sub: int
    dest: int
    source: int
    action: int
    payload: bytes

    def to_bytes(self) -> bytes:
        header = bytes([0xA5, self.sub, self.dest, self.source, self.action, len(self.payload)])
        body = header + self.payload
        chk = sum(body) & 0xFFFF
        return PREAMBLE + body + bytes([chk >> 8, chk & 0xFF])


@dataclass(frozen=True, slots=True)
class ChlorPacket:
    """An IntelliChlor frame (controller <-> chlorinator)."""

    dest: int
    action: int
    payload: bytes

    def to_bytes(self) -> bytes:
        body = bytes([0x10, 0x02, self.dest, self.action]) + self.payload
        return body + bytes([sum(body) & 0xFF, 0x10, 0x03])


def build_command(
    action: int,
    payload: bytes | list[int],
    source: int = DEFAULT_SOURCE_ADDR,
    sub: int = OUT_SUB_BYTE,
) -> bytes:
    """Frame a command addressed to the controller."""
    return Packet(sub, CONTROLLER_ADDR, source, action, bytes(payload)).to_bytes()


class FrameParser:
    """Turns an arbitrary byte stream into frames, skipping noise and bad checksums."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.bad_checksums = 0

    def feed(self, data: bytes) -> list[Packet | ChlorPacket]:
        self._buf.extend(data)
        out: list[Packet | ChlorPacket] = []
        while True:
            frame, consumed = self._next()
            if consumed:
                del self._buf[:consumed]
            if frame is not None:
                out.append(frame)
            elif not consumed:
                break
        # Never let garbage pile up while we wait for a header.
        if len(self._buf) > 1024:
            del self._buf[:-256]
        return out

    def _next(self) -> tuple[Packet | ChlorPacket | None, int]:
        """Return (frame or None, bytes to drop). (None, 0) means "need more data"."""
        buf = self._buf
        pre = _find_controller_start(buf)  # index of FF 00 FF A5, -1, or -2 (partial)
        ch = buf.find(b"\x10\x02")
        if ch >= 0 and (pre < 0 or ch < pre):
            if ch > 0:
                return None, ch  # drop leading noise
            return self._chlor_frame()
        if pre >= 0:
            if pre > 0:
                return None, pre
            return self._controller_frame()
        # Nothing yet; keep a short tail in case a preamble is split across reads.
        return None, max(0, len(buf) - 3)

    def _controller_frame(self) -> tuple[Packet | None, int]:
        buf = self._buf
        # buf starts with FF 00 FF A5.
        if len(buf) < 9:
            return None, 0
        length = buf[8]
        if length > MAX_PAYLOAD:
            return None, 1
        total = 9 + length + 2
        if len(buf) < total:
            return None, 0
        body = bytes(buf[3 : 9 + length])
        chk = (buf[9 + length] << 8) | buf[10 + length]
        if sum(body) & 0xFFFF != chk:
            self.bad_checksums += 1
            return None, 1
        return Packet(body[1], body[2], body[3], body[4], body[6:]), total

    def _chlor_frame(self) -> tuple[ChlorPacket | None, int]:
        buf = self._buf
        end = buf.find(b"\x10\x03", 4)
        if end < 0:
            if len(buf) > 40:
                return None, 2  # not a real chlorinator frame
            return None, 0
        body = bytes(buf[: end - 1])
        if len(body) < 4 or (sum(body) & 0xFF) != buf[end - 1]:
            self.bad_checksums += 1
            return None, 2
        return ChlorPacket(body[2], body[3], body[4:]), end + 2


def _find_controller_start(buf: bytearray) -> int:
    """Index of an FF 00 FF A5 sequence; -1 if none.

    A preamble right at the end of the buffer (A5 not read yet) also counts,
    so we wait for more data instead of throwing it away.
    """
    i = buf.find(PREAMBLE)
    while i >= 0:
        if i + 3 >= len(buf) or buf[i + 3] == 0xA5:
            return i
        i = buf.find(PREAMBLE, i + 1)
    return -1


# --------------------------------------------------------------------------- #
# Decoded state
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ControllerModel:
    """Personality board, from status bytes 27-28 (the same table njsPC uses)."""

    name: str
    relays: int  # aux relays: 4 or 8
    single_body: bool  # pool-only "P" models have no spa circuit

    def __str__(self) -> str:
        return self.name

    @property
    def missing_circuits(self) -> frozenset[int]:
        """Circuit numbers that don't exist on this panel."""
        missing: set[int] = set()
        if self.single_body:
            missing.add(1)  # the spa circuit; P models still name it "Pool"
        if self.relays == 4:
            missing.update((7, 8, 9))  # AUX 5-7
        return frozenset(missing)


# byte 28 = 13 (EasyTouch2) or 14 (EasyTouch); byte 27 = model within the family.
_EASYTOUCH_MODELS = {
    0: ("EasyTouch2 8", 8, False),
    1: ("EasyTouch2 8P", 8, True),
    2: ("EasyTouch2 4", 4, False),
    3: ("EasyTouch2 4P", 4, True),
    6: ("EasyTouch PSL4", 4, False),
    7: ("EasyTouch PL4", 4, True),
    128: ("EasyTouch 8", 8, False),
    129: ("EasyTouch 8P", 8, True),
    130: ("EasyTouch 4", 4, False),
    131: ("EasyTouch 4P", 4, True),
}


def decode_model(byte27: int, byte28: int) -> ControllerModel | None:
    if byte28 not in (13, 14):
        return None
    found = _EASYTOUCH_MODELS.get((128 if byte28 == 14 else 0) + byte27)
    return ControllerModel(*found) if found else None


@dataclass(slots=True)
class Status:
    """The controller's status broadcast (action 2)."""

    hour: int
    minute: int
    circuits_on: frozenset[int]
    mode_byte: int
    valve_byte: int
    heat_byte: int
    delay_byte: int
    water_raw: int
    air_raw: int
    raw: bytes

    @property
    def model(self) -> ControllerModel | None:
        if len(self.raw) < 29:
            return None
        return decode_model(self.raw[27], self.raw[28])

    @property
    def panel_mode(self) -> str:
        mode = self.mode_byte & 0x81
        try:
            return PANEL_MODE_NAMES[PanelMode(mode)]
        except ValueError:
            return f"unknown_{mode}"

    @property
    def service_mode(self) -> bool:
        """Panel in Service or Timeout: someone is working at the pool."""
        return (self.mode_byte & 0x81) != 0

    @property
    def freeze_protect(self) -> bool:
        return bool(self.mode_byte & 0x08)

    @property
    def start_timer(self) -> bool:
        """Bit 3 of the delay byte: the controller's 5-minute high-speed start timer.

        Set at every pool start and cleared five minutes later, when the
        controller drops the high-speed relay. njsPC doesn't decode it.
        """
        return bool(self.delay_byte & 0x08)

    @property
    def water_temp(self) -> int | None:
        return None if self.water_raw >= SENSOR_FAULT_MIN else self.water_raw

    @property
    def water_sensor_fault(self) -> bool:
        return self.water_raw >= SENSOR_FAULT_MIN

    @property
    def air_temp(self) -> int | None:
        return None if self.air_raw >= SENSOR_FAULT_MIN else self.air_raw

    def is_on(self, circuit: int) -> bool:
        return circuit in self.circuits_on


def decode_status(payload: bytes) -> Status | None:
    """Decode an action-2 payload; None if it is too short to be one."""
    if len(payload) < 19:
        return None
    on: set[int] = set()
    # Circuits 1-8 in byte 2, 9-16 in byte 3, ... up to byte 6.
    for byte_index in range(2, 7):
        byte = payload[byte_index]
        for bit in range(8):
            if byte & (1 << bit):
                on.add((byte_index - 2) * 8 + bit + 1)
    return Status(
        hour=payload[0],
        minute=payload[1],
        circuits_on=frozenset(on),
        mode_byte=payload[9],
        valve_byte=payload[10],
        heat_byte=payload[11],
        delay_byte=payload[12],
        water_raw=payload[14],
        air_raw=payload[18],
        raw=bytes(payload),
    )


@dataclass(frozen=True, slots=True)
class ScheduleSlot:
    """One of the controller's 12 schedule slots."""

    slot: int
    circuit: int  # 0 = empty
    start: int  # minutes after midnight
    end: int
    days: int  # DAY_BITS mask
    egg_timer: bool = False
    run_once: bool = False

    @property
    def empty(self) -> bool:
        return self.circuit == 0

    def same_schedule(self, other: ScheduleSlot | WantedSchedule) -> bool:
        return (
            not self.egg_timer
            and not self.run_once
            and self.circuit == other.circuit
            and self.start == other.start
            and self.end == other.end
            and self.days == other.days
        )


@dataclass(frozen=True, slots=True)
class WantedSchedule:
    """A repeating schedule HA wants on the controller."""

    circuit: int
    start: int
    end: int
    days: int

    def describe(self) -> str:
        return f"circuit {self.circuit} {fmt_minutes(self.start)}-{fmt_minutes(self.end)} {fmt_days(self.days)}"


def decode_schedule(payload: bytes) -> ScheduleSlot | None:
    """Decode an action-17 schedule-config payload."""
    if len(payload) < 7:
        return None
    slot = payload[0]
    circuit = payload[1] & 0x7F
    start_h, start_m, end_h, end_m, days = (
        payload[2],
        payload[3],
        payload[4],
        payload[5],
        payload[6],
    )
    if start_h == EGG_TIMER_MARKER and circuit:
        return ScheduleSlot(slot, circuit, 0, end_h * 60 + end_m, 0, egg_timer=True)
    if circuit == 0 or (start_h == 0 and start_m == 0 and end_h == 0 and end_m == 0):
        return ScheduleSlot(slot, 0, 0, 0, 0)
    run_once = end_h == 26
    end = 0 if run_once else end_h * 60 + end_m
    return ScheduleSlot(slot, circuit, start_h * 60 + start_m, end, days & 0x7F, run_once=run_once)


@dataclass(frozen=True, slots=True)
class CircuitInfo:
    """A circuit as configured on the controller (action 11)."""

    circuit: int
    function: int
    name_id: int
    freeze: bool

    @property
    def in_use(self) -> bool:
        return self.function != CIRCUIT_FUNCTION_NOT_USED and self.name_id != 0

    @property
    def custom_name_slot(self) -> int | None:
        return self.name_id - CUSTOM_NAME_BASE if self.name_id >= CUSTOM_NAME_BASE else None

    def name(self, custom_names: dict[int, str]) -> str:
        slot = self.custom_name_slot
        if slot is not None:
            return custom_names.get(slot) or f"Circuit {self.circuit}"
        if 0 < self.name_id < len(CIRCUIT_NAMES):
            return CIRCUIT_NAMES[self.name_id]
        return f"Circuit {self.circuit}"


def decode_circuit_config(payload: bytes) -> CircuitInfo | None:
    if len(payload) < 3 or not 1 <= payload[0] <= MAX_CIRCUIT_ID:
        return None
    return CircuitInfo(payload[0], payload[1] & 63, payload[2], bool(payload[1] & 64))


def decode_custom_name(payload: bytes) -> tuple[int, str] | None:
    if len(payload) < 2:
        return None
    text = bytes(b for b in payload[1:12] if 32 <= b < 127).decode("ascii").strip()
    return payload[0], text


@dataclass(slots=True)
class ChlorState:
    salt_ppm: int | None = None
    status_byte: int | None = None
    output_pct: int | None = None
    # From the controller's chlorinator config (action 25):
    present: bool = False
    pool_setpoint: int | None = None
    spa_setpoint: int | None = None
    super_chlor_hours: int = 0  # hours left; 0 = not super-chlorinating

    @property
    def problems(self) -> list[str]:
        if self.status_byte is None:
            return []
        return [name for bit, name in CHLOR_STATUS_FLAGS if self.status_byte & bit]


def decode_chlor_status(payload: bytes, chlor: ChlorState) -> None:
    """Controller's chlorinator summary (action 25)."""
    if len(payload) < 5 or not payload[0] & 0x01:
        return
    chlor.present = True
    chlor.spa_setpoint = payload[0] >> 1
    chlor.pool_setpoint = payload[1]
    if len(payload) > 5:
        chlor.super_chlor_hours = payload[5]
    if payload[3]:
        chlor.salt_ppm = payload[3] * 50
    chlor.status_byte = payload[4] & 0x7F


def decode_chlor_frame(frame: ChlorPacket, chlor: ChlorState) -> None:
    """Direct IntelliChlor traffic."""
    if frame.action == 0x11 and frame.payload:  # controller -> cell: output %
        chlor.output_pct = frame.payload[0]
    elif frame.action == 0x12 and len(frame.payload) >= 2:  # cell -> controller: salt, status
        if frame.payload[0]:
            chlor.salt_ppm = frame.payload[0] * 50
        chlor.status_byte = frame.payload[1] & 0xFF


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_set_circuit(circuit: int, on: bool) -> bytes:
    return bytes([circuit, 1 if on else 0])


def cmd_set_schedule(slot: int, wanted: WantedSchedule | None) -> bytes:
    """Write a repeating schedule into a slot, or clear it (wanted=None)."""
    if wanted is None:
        return bytes([slot, 0, 0, 0, 0, 0, 0])
    return bytes(
        [
            slot,
            wanted.circuit,
            wanted.start // 60,
            wanted.start % 60,
            wanted.end // 60,
            wanted.end % 60,
            wanted.days & 0x7F,
        ]
    )


def cmd_set_chlor(pool_setpoint: int, spa_setpoint: int, super_hours: int) -> bytes:
    """Action 153: keep the output setpoints, start (hours > 0) or stop super-chlorinate."""
    return bytes(
        [
            (spa_setpoint << 1) + 1,
            pool_setpoint,
            super_hours + 128 if super_hours > 0 else 0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]
    )


def cmd_get_schedule(slot: int) -> bytes:
    return bytes([slot])


def cmd_set_clock(now: datetime, auto_dst: bool = True) -> bytes:
    """Payload for action 133: hour, minute, day bit, date, month, year, 0, DST flag."""
    day_bit = 1 << ((now.weekday() + 1) % 7)  # Monday=0 -> mon bit (2); Sunday -> 1
    return bytes(
        [now.hour, now.minute, day_bit, now.day, now.month, now.year - 2000, 0, int(auto_dst)]
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def fmt_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def fmt_days(mask: int) -> str:
    if mask == EVERY_DAY:
        return "every day"
    if mask == WEEKDAYS:
        return "Mon-Fri"
    if mask == WEEKEND:
        return "Sat/Sun"
    return ",".join(name for name, bit in DAY_BITS.items() if mask & bit) or "no days"


@dataclass(slots=True)
class SchedulePlan:
    """What to write so the controller holds exactly the wanted schedules."""

    writes: list[tuple[int, WantedSchedule]] = field(default_factory=list)
    clears: list[int] = field(default_factory=list)
    unplaced: list[WantedSchedule] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.writes or self.clears or self.unplaced)


def plan_schedules(current: dict[int, ScheduleSlot], wanted: list[WantedSchedule]) -> SchedulePlan:
    """Diff the controller's slots against what HA wants.

    Egg timers are never touched. Any other schedule that isn't wanted is
    cleared. Wanted schedules that already exist stay in their slot; the rest
    go into free slots (lowest first).
    """
    plan = SchedulePlan()
    remaining = list(dict.fromkeys(wanted))  # drop exact duplicates, keep order
    keep: set[int] = set()
    for slot_id in sorted(current):
        slot = current[slot_id]
        if slot.egg_timer or slot.empty:
            continue
        match = next((w for w in remaining if slot.same_schedule(w)), None)
        if match is not None:
            remaining.remove(match)
            keep.add(slot_id)
        else:
            plan.clears.append(slot_id)
    free = sorted(
        s
        for s in range(1, MAX_SCHEDULE_SLOTS + 1)
        if s not in keep and not (s in current and current[s].egg_timer)
    )
    for w in remaining:
        if free:
            slot_id = free.pop(0)
            plan.writes.append((slot_id, w))
            if slot_id in plan.clears:
                plan.clears.remove(slot_id)  # the write replaces it
        else:
            plan.unplaced.append(w)
    return plan
