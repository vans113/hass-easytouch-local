"""End-to-end through Home Assistant against the fake EasyTouch."""

from __future__ import annotations

import asyncio
from datetime import datetime
from functools import partial

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.easytouch_local import client as client_mod
from custom_components.easytouch_local.const import (
    CONF_POWER_ENTITY,
    CONF_READ_ONLY,
    CONF_SCHEDULE_MODE,
    DEFAULT_OPTIONS,
    DOMAIN,
    SCHEDULE_MODE_CONTROLLER,
)

from .fake_controller import FakeEasyTouch
from .test_client import FAST

POWER = "sensor.pool_power"


@pytest.fixture
async def fake():
    f = FakeEasyTouch(interval=0.05)
    await f.start()
    yield f
    await f.stop()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    """Shrink every delay so tests run in seconds."""
    import custom_components.easytouch_local as integ
    import custom_components.easytouch_local.config_flow as flow
    import custom_components.easytouch_local.coordinator as coord

    fast_client = partial(client_mod.EasyTouchClient, timing=FAST)
    monkeypatch.setattr(integ, "EasyTouchClient", lambda *a, **k: fast_client(*a, **k))
    monkeypatch.setattr(flow, "EasyTouchClient", lambda *a, **k: fast_client(*a, **k))
    monkeypatch.setattr(flow, "PROBE_WAIT", 1.0)
    monkeypatch.setattr(integ, "SETUP_WAIT", 2.0)
    monkeypatch.setattr(coord, "RECONNECT_SETTLE", 0.05)
    monkeypatch.setattr(coord, "SCHEDULE_DEBOUNCE", 0.05)
    monkeypatch.setattr(coord, "POWER_GRACE", 0.0)
    monkeypatch.setattr(coord, "CLOCK_ALIGN", False)


def _helpers(hass: HomeAssistant) -> None:
    for key, value in {
        "input_boolean.pool_pump_schedule_enabled": "on",
        "input_datetime.pool_summer_weekday_start": "09:00:00",
        "input_datetime.pool_summer_weekday_stop": "12:55:00",
        "input_datetime.pool_summer_weekend_start": "09:00:00",
        "input_datetime.pool_summer_weekend_stop": "17:00:00",
        "input_datetime.pool_winter_start": "10:05:00",
        "input_datetime.pool_winter_stop": "16:00:00",
    }.items():
        hass.states.async_set(key, value)


async def _setup(hass: HomeAssistant, fake: FakeEasyTouch, **options) -> MockConfigEntry:
    hass.config.units = US_CUSTOMARY_SYSTEM  # the controller reports Fahrenheit
    _helpers(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"127.0.0.1:{fake.port}",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake.port},
        options={**DEFAULT_OPTIONS, CONF_POWER_ENTITY: "", **options},
        title="Pool controller",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _until(hass: HomeAssistant, pred, limit: float = 4.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while loop.time() < end:
        await hass.async_block_till_done()
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def _eid(hass: HomeAssistant, platform: str, key: str) -> str:
    entry = er.async_get(hass)
    for e in entry.entities.values():
        if e.platform == DOMAIN and e.domain == platform and e.unique_id.endswith(f"_{key}"):
            return e.entity_id
    raise AssertionError(f"no {platform} entity for {key}")


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------- #
async def test_listen_only_reads_but_never_sends(hass: HomeAssistant, fake: FakeEasyTouch):
    fake.state.circuits = {6}
    entry = await _setup(hass, fake)  # default: listen only
    assert entry.state is ConfigEntryState.LOADED
    pool = _eid(hass, "switch", "circuit_6")
    assert hass.states.get(pool).state == STATE_ON
    assert hass.states.get(_eid(hass, "sensor", "air_temp")).state == "91"
    assert hass.states.get(_eid(hass, "binary_sensor", "water_sensor_fault")).state == STATE_ON
    with pytest.raises(HomeAssistantError, match="listen-only"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, "switch", "pool_pump")}, blocking=True
        )
    await asyncio.sleep(0.2)
    assert fake.received == []
    await _unload(hass, entry)


async def test_pool_pump_drives_both_circuits(hass: HomeAssistant, fake: FakeEasyTouch):
    entry = await _setup(hass, fake, **{CONF_READ_ONLY: False})
    pump = _eid(hass, "switch", "pool_pump")
    assert hass.states.get(pump).state == STATE_OFF
    await hass.services.async_call("switch", "turn_on", {"entity_id": pump}, blocking=True)
    assert fake.state.circuits == {2, 6}
    assert await _until(hass, lambda: hass.states.get(pump).state == STATE_ON)
    await hass.services.async_call("switch", "turn_off", {"entity_id": pump}, blocking=True)
    assert fake.state.circuits == set()
    assert await _until(hass, lambda: hass.states.get(pump).state == STATE_OFF)
    await _unload(hass, entry)


async def test_pump_shows_off_when_relay_on_but_no_power(hass: HomeAssistant, fake: FakeEasyTouch):
    fake.state.circuits = {2, 6}
    hass.states.async_set(POWER, "1620")
    entry = await _setup(hass, fake, **{CONF_POWER_ENTITY: POWER})
    pump = _eid(hass, "switch", "pool_pump")
    assert await _until(hass, lambda: hass.states.get(pump).state == STATE_ON)
    hass.states.async_set(POWER, "20")
    assert await _until(hass, lambda: hass.states.get(pump).state == STATE_OFF)
    assert hass.states.get(pump).attributes["high_speed_relay"] is True
    await _unload(hass, entry)


async def test_silent_link_goes_unavailable_and_recovers_without_reload(
    hass: HomeAssistant, fake: FakeEasyTouch
):
    entry = await _setup(hass, fake)
    air = _eid(hass, "sensor", "air_temp")
    link = _eid(hass, "binary_sensor", "connected")
    fake.silent = True
    coordinator = entry.runtime_data
    assert await _until(
        hass,
        lambda: (
            coordinator._handle_client_event() or hass.states.get(air).state == STATE_UNAVAILABLE
        ),
    )
    assert await _until(hass, lambda: hass.states.get(link).state == STATE_OFF)
    fake.silent = False
    assert await _until(hass, lambda: hass.states.get(air).state == "91")
    assert await _until(hass, lambda: hass.states.get(link).state == STATE_ON)
    assert entry.state is ConfigEntryState.LOADED  # never reloaded
    assert coordinator.client.stats.watchdog_trips >= 1
    await _unload(hass, entry)


async def test_ha_mode_clears_stray_schedules_but_keeps_egg_timers(
    hass: HomeAssistant, fake: FakeEasyTouch
):
    fake.state.schedules[1] = bytes([1, 6, 9, 0, 12, 55, 62])  # someone added one at the panel
    entry = await _setup(hass, fake, **{CONF_READ_ONLY: False})
    coordinator = entry.runtime_data
    assert await _until(hass, lambda: coordinator.schedule_sync.state == "in_sync")
    assert 1 not in fake.state.schedules
    assert set(fake.state.schedules) == {7, 8}  # egg timers untouched
    await _unload(hass, entry)


async def test_controller_mode_writes_and_verifies_schedules(
    hass: HomeAssistant, fake: FakeEasyTouch, monkeypatch
):
    import custom_components.easytouch_local.coordinator as coord

    # October counts as summer (Apr-Oct) for the pump windows.
    monkeypatch.setattr(coord, "_now", lambda: datetime(2026, 10, 26, 8, 0))
    entry = await _setup(hass, fake, **{CONF_READ_ONLY: False})
    coordinator = entry.runtime_data
    assert await _until(hass, lambda: coordinator.schedule_sync.state == "in_sync")
    select = _eid(hass, "select", "schedule_mode")
    fake.drop_writes[145] = 1  # first write silently lost; must be caught by read-back
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select, "option": SCHEDULE_MODE_CONTROLLER},
        blocking=True,
    )
    assert await _until(
        hass,
        lambda: coordinator.schedule_sync.state == "in_sync" and len(fake.state.schedules) == 6,
        6,
    )
    written = {v[1:] for k, v in fake.state.schedules.items() if k not in (7, 8)}
    assert written == {
        bytes([6, 9, 0, 12, 55, 62]),  # Pool, Mon-Fri 9:00-12:55
        bytes([2, 9, 0, 12, 55, 62]),  # High speed, same
        bytes([6, 9, 0, 17, 0, 65]),  # Pool, Sat/Sun 9:00-17:00
        bytes([2, 9, 0, 17, 0, 65]),  # High speed, same
    }
    assert set(fake.state.schedules) >= {7, 8}  # egg timers kept
    assert entry.options[CONF_SCHEDULE_MODE] == SCHEDULE_MODE_CONTROLLER
    assert entry.state is ConfigEntryState.LOADED  # select doesn't reload

    # Changing a helper rewrites just that window.
    hass.states.async_set("input_datetime.pool_summer_weekday_stop", "12:30:00")
    assert await _until(
        hass,
        lambda: (
            any(v[1:6] == bytes([6, 9, 0, 12, 30]) for v in fake.state.schedules.values())
            and coordinator.schedule_sync.state == "in_sync"
        ),
        6,
    )
    # Schedule switched off: everything but the egg timers goes.
    hass.states.async_set("input_boolean.pool_pump_schedule_enabled", "off")
    assert await _until(hass, lambda: set(fake.state.schedules) == {7, 8}, 6)
    await _unload(hass, entry)


async def test_schedule_failure_raises_repair_and_problem(hass: HomeAssistant, fake: FakeEasyTouch):
    fake.state.schedules[1] = bytes([1, 6, 9, 0, 12, 55, 62])
    fake.drop_writes[145] = 99  # never sticks
    entry = await _setup(hass, fake, **{CONF_READ_ONLY: False})
    coordinator = entry.runtime_data
    problem = _eid(hass, "binary_sensor", "schedule_problem")
    assert await _until(hass, lambda: coordinator.schedule_sync.state == "failed", 8)
    assert hass.states.get(problem).state == STATE_ON
    from homeassistant.helpers import issue_registry as ir

    assert ir.async_get(hass).async_get_issue(DOMAIN, "schedule_sync_failed")
    await _unload(hass, entry)


async def test_service_mode_blocks_commands(hass: HomeAssistant, fake: FakeEasyTouch):
    fake.state.mode = 0x01
    entry = await _setup(hass, fake, **{CONF_READ_ONLY: False})
    with pytest.raises(HomeAssistantError, match="Service"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, "switch", "pool_pump")}, blocking=True
        )
    assert not fake.commands(134)
    await _unload(hass, entry)


async def test_config_flow(hass: HomeAssistant, fake: FakeEasyTouch):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: fake.port, CONF_READ_ONLY: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_READ_ONLY] is True
    assert fake.received == []
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await _unload(hass, entry)


async def test_config_flow_no_status(hass: HomeAssistant, fake: FakeEasyTouch):
    fake.silent = True
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: fake.port}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_status"}


async def test_setup_survives_bridge_down(hass: HomeAssistant, fake: FakeEasyTouch):
    port = fake.port
    await fake.stop()
    entry = await _setup(hass, fake)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(_eid(hass, "sensor", "air_temp")).state == STATE_UNAVAILABLE
    # Bridge comes back on the same port: it reconnects by itself.
    fake.server = await asyncio.start_server(fake._client, "127.0.0.1", port)
    fake._tasks.append(asyncio.get_running_loop().create_task(fake._broadcast_loop()))
    assert await _until(
        hass, lambda: hass.states.get(_eid(hass, "sensor", "air_temp")).state == "91", 6
    )
    await _unload(hass, entry)
