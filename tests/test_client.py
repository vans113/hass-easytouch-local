"""The connection: watchdog, reconnects and confirmed commands."""

import asyncio
from datetime import datetime

import pytest

from custom_components.easytouch_local.client import (
    CommandNotConfirmedError,
    EasyTouchClient,
    ReadOnlyError,
    Timing,
)
from custom_components.easytouch_local.protocol import WEEKDAYS, Action, WantedSchedule

from .fake_controller import FakeEasyTouch

FAST = Timing(
    connect_timeout=1,
    heartbeat_timeout=0.4,
    stale_after=0.8,
    ack_timeout=0.2,
    confirm_timeout=0.4,
    schedule_read_timeout=0.3,
    command_gap=0.02,
    bus_quiet=0.005,
    bus_quiet_max=0.05,
    backoff=(0.05, 0.1),
)


@pytest.fixture
async def fake():
    f = FakeEasyTouch(interval=0.05)
    await f.start()
    yield f
    await f.stop()


@pytest.fixture
async def client(fake):
    c = EasyTouchClient("127.0.0.1", fake.port, read_only=False, timing=FAST)
    c.start()
    assert await c.wait_for_status(2)
    yield c
    await c.stop()


async def _until(pred, limit=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_reads_status_and_chlorinator(client):
    assert client.available and client.connected
    assert client.status.air_temp == 91
    assert await _until(lambda: client.chlor.salt_ppm == 2800)
    assert client.chlor.output_pct == 30


async def test_set_circuit_is_confirmed(client, fake):
    await client.set_circuit(2, True)
    assert 2 in fake.state.circuits
    await client.set_circuit(2, False)
    assert 2 not in fake.state.circuits


async def test_dropped_write_is_resent(client, fake):
    fake.drop_writes[Action.SET_CIRCUIT] = 1
    await client.set_circuit(6, True)
    assert len(fake.commands(Action.SET_CIRCUIT)) == 2
    assert client.stats.commands_retried == 1


async def test_write_that_never_sticks_raises(client, fake):
    fake.drop_writes[Action.SET_CIRCUIT] = 99
    with pytest.raises(CommandNotConfirmedError):
        await client.set_circuit(6, True)
    assert client.stats.commands_failed == 1


async def test_read_only_never_sends(fake):
    c = EasyTouchClient("127.0.0.1", fake.port, read_only=True, timing=FAST)
    c.start()
    try:
        assert await c.wait_for_status(2)
        with pytest.raises(ReadOnlyError):
            await c.set_circuit(2, True)
        await asyncio.sleep(0.1)
        assert fake.received == []
    finally:
        await c.stop()


async def test_silent_link_trips_watchdog_and_recovers(client, fake):
    fake.silent = True  # socket stays open, nothing arrives (the scary case)
    assert await _until(lambda: not client.available, 2)
    assert client.stats.watchdog_trips >= 1
    fake.silent = False
    assert await _until(lambda: client.available, 3)
    assert client.stats.connects >= 2  # it reconnected on its own


async def test_bridge_drop_reconnects(client, fake):
    await fake.kick_clients()
    assert await _until(lambda: client.stats.connects >= 2 and client.available, 3)


async def test_schedule_write_is_read_back(client, fake):
    wanted = WantedSchedule(6, 540, 775, WEEKDAYS)
    fake.drop_writes[Action.SET_SCHEDULE] = 1  # first write silently lost
    slot = await client.write_schedule(1, wanted)
    assert slot.same_schedule(wanted)
    assert fake.state.schedules[1] == bytes([1, 6, 9, 0, 12, 55, 62])
    assert len(fake.commands(Action.SET_SCHEDULE)) == 2
    await client.write_schedule(1, None)
    assert 1 not in fake.state.schedules


async def test_read_all_schedules_sees_egg_timers(client):
    slots = await client.read_all_schedules()
    assert len(slots) == 12
    assert slots[7].egg_timer and slots[8].egg_timer
    assert slots[1].empty


async def test_set_clock_confirmed(client, fake):
    await client.set_clock(datetime(2026, 10, 26, 3, 0))
    assert (fake.state.hour, fake.state.minute) == (3, 0)


async def test_copies_header_byte_from_controller(client, fake):
    """The panel ignores commands whose header byte differs from its broadcasts."""
    assert client.sub_byte == 1
    await client.set_circuit(6, True)
    assert 6 in fake.state.circuits
    assert not fake.wrong_header
