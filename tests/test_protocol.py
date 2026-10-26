"""Framing, decoding and schedule planning."""

from datetime import datetime

from custom_components.easytouch_local.protocol import (
    EVERY_DAY,
    WEEKDAYS,
    WEEKEND,
    ChlorPacket,
    ChlorState,
    FrameParser,
    Packet,
    ScheduleSlot,
    WantedSchedule,
    build_command,
    cmd_set_clock,
    cmd_set_schedule,
    decode_chlor_frame,
    decode_schedule,
    decode_status,
    plan_schedules,
)

from .fake_controller import REAL_STATUS


def test_command_checksums_match_known_packets():
    # From njsPC's EasyTouch notes: a clock set from address 34, and its ack.
    clock = Packet(33, 16, 34, 133, bytes([13, 10, 16, 29, 8, 19, 0, 0])).to_bytes()
    assert clock[-2:] == bytes([1, 228])
    ack = Packet(33, 34, 16, 1, bytes([133])).to_bytes()
    assert ack[-2:] == bytes([1, 127])
    # build_command addresses the controller from our slot.
    pkt = build_command(134, [2, 1], source=33)
    assert pkt[:9] == bytes([0xFF, 0x00, 0xFF, 0xA5, 33, 16, 33, 134, 2])


def test_parser_handles_noise_split_reads_and_bad_checksums():
    good = Packet(1, 15, 16, 2, REAL_STATUS).to_bytes()
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    stream = (
        b"\x00\xfe\x10"
        + bytes(bad)
        + b"\x99"
        + good
        + ChlorPacket(0, 0x12, bytes([56, 2])).to_bytes()
    )
    parser = FrameParser()
    frames = []
    for i in range(0, len(stream), 7):  # dribble it in
        frames += parser.feed(stream[i : i + 7])
    assert parser.bad_checksums >= 1
    assert [type(f).__name__ for f in frames] == ["Packet", "ChlorPacket"]
    assert frames[0].payload == REAL_STATUS


def test_decode_real_status_frame():
    s = decode_status(REAL_STATUS)
    assert s is not None
    assert (s.hour, s.minute) == (13, 8)
    assert s.circuits_on == {6}
    assert s.is_on(6) and not s.is_on(2)
    assert s.start_timer  # 0x08 in byte 12: the 5-minute high-speed timer
    assert s.panel_mode == "auto" and not s.service_mode
    assert s.water_sensor_fault and s.water_temp is None
    assert s.air_temp == 91


def test_decode_status_service_mode_and_high_circuits():
    p = bytearray(REAL_STATUS)
    p[9] = 0x81
    p[3] = 0b0000_0100  # circuit 11
    s = decode_status(bytes(p))
    assert s.service_mode and s.panel_mode == "service_timeout"
    assert 11 in s.circuits_on


def test_decode_schedules():
    egg = decode_schedule(bytes([7, 2, 25, 0, 12, 0, 0]))
    assert egg.egg_timer and egg.circuit == 2
    empty = decode_schedule(bytes([3, 0, 0, 0, 0, 0, 0]))
    assert empty.empty
    sched = decode_schedule(bytes([1, 6, 9, 0, 12, 55, 62]))
    assert (sched.circuit, sched.start, sched.end, sched.days) == (6, 540, 775, WEEKDAYS)
    assert cmd_set_schedule(1, WantedSchedule(6, 540, 775, WEEKDAYS)) == bytes(
        [1, 6, 9, 0, 12, 55, 62]
    )
    assert cmd_set_schedule(4, None) == bytes([4, 0, 0, 0, 0, 0, 0])


def test_clock_payload_day_bits():
    # 2026-10-25 is a Sunday; 2026-10-26 a Monday.
    assert cmd_set_clock(datetime(2026, 10, 25, 12, 15))[:6] == bytes([12, 15, 1, 25, 10, 26])
    assert cmd_set_clock(datetime(2026, 10, 26, 3, 0))[2] == 2


def test_chlorinator_frames():
    chlor = ChlorState()
    decode_chlor_frame(ChlorPacket(0x50, 0x11, bytes([30])), chlor)
    decode_chlor_frame(ChlorPacket(0x00, 0x12, bytes([56, 0x03])), chlor)
    assert chlor.output_pct == 30
    assert chlor.salt_ppm == 2800
    assert chlor.problems == ["low_flow", "low_salt"]


def _slot(slot, circuit, start, end, days):
    return ScheduleSlot(slot, circuit, start, end, days)


def test_plan_keeps_matches_clears_strays_and_never_touches_egg_timers():
    current = {i: ScheduleSlot(i, 0, 0, 0, 0) for i in range(1, 13)}
    current[1] = _slot(1, 6, 540, 775, WEEKDAYS)  # wanted, keep
    current[2] = _slot(2, 6, 600, 700, EVERY_DAY)  # stray, clear
    current[7] = ScheduleSlot(7, 2, 0, 720, 0, egg_timer=True)
    wanted = [
        WantedSchedule(6, 540, 775, WEEKDAYS),
        WantedSchedule(2, 540, 775, WEEKDAYS),
        WantedSchedule(6, 540, 1020, WEEKEND),
        WantedSchedule(2, 540, 1020, WEEKEND),
    ]
    plan = plan_schedules(current, wanted)
    written = {slot for slot, _ in plan.writes}
    assert 7 not in written and 7 not in plan.clears
    assert 1 not in written and 1 not in plan.clears
    assert 2 in written  # reused for a wanted schedule, so not separately cleared
    assert 2 not in plan.clears
    assert len(plan.writes) == 3 and not plan.unplaced


def test_plan_empty_when_in_sync_and_clears_everything_for_no_schedules():
    current = {i: ScheduleSlot(i, 0, 0, 0, 0) for i in range(1, 13)}
    current[3] = _slot(3, 6, 540, 775, WEEKDAYS)
    assert plan_schedules(current, [WantedSchedule(6, 540, 775, WEEKDAYS)]).empty
    plan = plan_schedules(current, [])
    assert plan.clears == [3] and not plan.writes


def test_model_from_status():
    from custom_components.easytouch_local.protocol import decode_model

    m = decode_status(REAL_STATUS).model
    assert m is not None and m.name == "EasyTouch2 4P"
    assert m.missing_circuits == {1, 7, 8, 9}
    assert decode_model(0, 13).missing_circuits == frozenset()
    assert decode_model(2, 99) is None
