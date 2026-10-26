"""Coordinator: owns the link, the pool-pump logic, schedule sync and clock sync."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as dt_time
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_ON
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import EasyTouchClient, EasyTouchError, ReadOnlyError
from .const import (
    CLOCK_DRIFT_LIMIT,
    CLOCK_MIN_RESYNC,
    CONF_CIRCUIT_NAMES,
    CONF_POWER_ENTITY,
    CONF_POWER_THRESHOLD,
    CONF_PUMP_CIRCUITS,
    CONF_READ_ONLY,
    CONF_SCHEDULE_ENABLED_ENTITY,
    CONF_SCHEDULE_MODE,
    CONF_SUMMER_MONTHS,
    CONF_SUMMER_WEEKDAY_START,
    CONF_SUMMER_WEEKDAY_STOP,
    CONF_SUMMER_WEEKEND_START,
    CONF_SUMMER_WEEKEND_STOP,
    CONF_WINTER_START,
    CONF_WINTER_STOP,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOGGER,
    POWER_GRACE,
    POWER_LIVE_WINDOW,
    RECONNECT_SETTLE,
    REFRESH_INTERVAL,
    SCHEDULE_CHECK_INTERVAL,
    SCHEDULE_DEBOUNCE,
    SCHEDULE_MODE_CONTROLLER,
    SCHEDULE_RETRY_DELAY,
    TIME_HELPER_KEYS,
)
from .protocol import (
    EVERY_DAY,
    WEEKDAYS,
    WEEKEND,
    ScheduleSlot,
    Status,
    WantedSchedule,
    fmt_days,
    fmt_minutes,
    plan_schedules,
)

if TYPE_CHECKING:
    from . import EasyTouchConfigEntry

SCHEDULE_ISSUE = "schedule_sync_failed"
CLOCK_ALIGN = True  # wait for a minute boundary before setting the clock (tests turn off)


@dataclass(slots=True)
class ScheduleSyncState:
    state: str = "unknown"  # in_sync | syncing | failed | listen_only | paused | unknown
    error: str | None = None
    last_success: datetime | None = None
    last_attempt: datetime | None = None


def _now() -> datetime:
    """Local time (a seam for tests)."""
    return dt_util.now()


def parse_months(text: str) -> set[int]:
    """'4-10' or '4,5,6' -> month numbers."""
    months: set[int] = set()
    for part in str(text).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            rng = range(a, b + 1) if a <= b else [*range(a, 13), *range(1, b + 1)]
            months.update(rng)
        else:
            months.add(int(part))
    return {m for m in months if 1 <= m <= 12}


def parse_circuit_list(text: str) -> list[int]:
    """'6, 2' -> [6, 2], in order, no duplicates."""
    out: list[int] = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in out and 1 <= int(part) <= 40:
            out.append(int(part))
    return out


def parse_circuit_names(text: str) -> dict[int, str]:
    """'3:AUX 2, 4:Lights' -> {3: 'AUX 2', 4: 'Lights'}."""
    out: dict[int, str] = {}
    for part in str(text).split(","):
        if ":" not in part:
            continue
        num, name = part.split(":", 1)
        try:
            out[int(num.strip())] = name.strip() or f"Circuit {num.strip()}"
        except ValueError:
            continue
    return out


class EasyTouchCoordinator(DataUpdateCoordinator[Status | None]):
    """Pushes controller updates to entities and runs the background jobs."""

    config_entry: EasyTouchConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: EasyTouchConfigEntry, client: EasyTouchClient
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.host}",
            update_interval=REFRESH_INTERVAL,
            always_update=True,  # the client only notifies on real changes
        )
        self.client = client
        self.schedule_sync = ScheduleSyncState()
        self._schedule_lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        self._schedule_timer: Callable[[], None] | None = None
        self._was_available = False
        self._pump_changed_at = time.monotonic()
        self._last_pump_relays: tuple[bool, ...] | None = None
        self._known_circuits: set[int] = set()
        self._resync_task: asyncio.Task[None] | None = None
        self._resync_again = False
        self._seen_connects = 0
        self._last_clock_sync = 0.0
        self.clock_drift: int | None = None
        self.suppress_reload = False  # set by the schedule-mode select
        self._clock_task: asyncio.Task[None] | None = None
        self._remove_listener = client.add_listener(self._handle_client_event)

    # ------------------------------------------------------------------ #
    # Options
    # ------------------------------------------------------------------ #
    def opt(self, key: str) -> Any:
        return self.config_entry.options.get(key, DEFAULT_OPTIONS[key])

    @property
    def pump_circuits(self) -> list[int]:
        """Circuits the pool pump switch turns on together, in order (off in reverse)."""
        return parse_circuit_list(self.opt(CONF_PUMP_CIRCUITS)) or [6]

    @property
    def new_circuits_signal(self) -> str:
        return f"{DOMAIN}_{self.config_entry.entry_id}_new_circuits"

    def circuit_ids(self) -> list[int]:
        """Every circuit that gets a switch.

        Circuits the controller reports as in use, the pump circuits, anything
        named in the options, and anything seen switched on.
        """
        ids = {c for c, info in self.client.circuits.items() if info.in_use}
        ids.update(self.pump_circuits)
        ids.update(parse_circuit_names(self.opt(CONF_CIRCUIT_NAMES)))
        if self.client.status is not None:
            ids.update(c for c in self.client.status.circuits_on if c <= 18)
        ids -= self.missing_circuits()
        return sorted(ids)

    def missing_circuits(self) -> set[int]:
        """Circuits this panel model doesn't have (unless the user names them)."""
        status = self.client.status
        model = status.model if status is not None else None
        if model is None:
            return set()
        keep = set(self.pump_circuits) | set(parse_circuit_names(self.opt(CONF_CIRCUIT_NAMES)))
        return set(model.missing_circuits) - keep

    def circuit_name(self, circuit: int) -> str:
        override = parse_circuit_names(self.opt(CONF_CIRCUIT_NAMES)).get(circuit)
        if override:
            return override
        info = self.client.circuits.get(circuit)
        if info is not None and info.in_use:
            return info.name(self.client.custom_names)
        return {6: "Pool", 1: "Spa"}.get(circuit, f"Circuit {circuit}")

    @property
    def circuit_names(self) -> dict[int, str]:
        return {c: self.circuit_name(c) for c in self.circuit_ids()}

    @property
    def read_only(self) -> bool:
        return bool(self.opt(CONF_READ_ONLY))

    @property
    def schedule_mode(self) -> str:
        return str(self.opt(CONF_SCHEDULE_MODE))

    @property
    def unique_id(self) -> str:
        return self.config_entry.unique_id or f"{self.client.host}:{self.client.port}"

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @callback
    def async_start_jobs(self) -> None:
        """Background jobs; called once entities are set up."""
        entry = self.config_entry
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._interval_schedule_check, SCHEDULE_CHECK_INTERVAL
            )
        )
        # Nightly clock reset (also lands the DST change), and drift checks each minute.
        self._unsubs.append(
            async_track_time_change(self.hass, self._nightly_clock, hour=3, minute=0, second=5)
        )
        self._unsubs.append(async_track_time_change(self.hass, self._minute_tick, second=20))
        watched = [self.opt(k) for k in (*TIME_HELPER_KEYS, CONF_SCHEDULE_ENABLED_ENTITY)]
        watched = [e for e in watched if e]
        if watched:
            self._unsubs.append(
                async_track_state_change_event(self.hass, watched, self._helper_changed)
            )
        if power := self.opt(CONF_POWER_ENTITY):
            self._unsubs.append(
                async_track_state_change_event(self.hass, [power], self._power_changed)
            )
        entry.async_on_unload(self._cancel_jobs)
        if self.client.available:
            self._on_became_available()

    @callback
    def _cancel_jobs(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._schedule_timer:
            self._schedule_timer()
            self._schedule_timer = None

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        self._cancel_jobs()
        self._remove_listener()
        await self.client.stop()

    # ------------------------------------------------------------------ #
    # Data flow
    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> Status | None:
        """Runs every few seconds: notices a link that died without a frame."""
        self._check_transition()
        if not self.client.available:
            raise UpdateFailed("no recent status from the EasyTouch")
        return self.client.status

    @callback
    def _handle_client_event(self) -> None:
        status = self.client.status
        if status is not None:
            relays = tuple(status.is_on(c) for c in self.pump_circuits)
            if relays != self._last_pump_relays:
                self._last_pump_relays = relays
                self._pump_changed_at = time.monotonic()
        self._check_new_circuits()
        self._check_transition()
        if self.client.available:
            self.async_set_updated_data(status)
        else:
            self.async_update_listeners()

    @callback
    def _check_transition(self) -> None:
        available = self.client.available
        connects = self.client.stats.connects
        if available and not self._was_available:
            self._was_available = True
            self._seen_connects = connects
            LOGGER.info("EasyTouch status is flowing")
            self._on_became_available()
        elif available and connects != self._seen_connects:
            # A quick reconnect that never made entities unavailable still
            # gets the full re-read.
            self._seen_connects = connects
            self._on_became_available()
        elif not available and self._was_available:
            self._was_available = False
            LOGGER.warning("EasyTouch went quiet; entities unavailable until it's back")

    @callback
    def _check_new_circuits(self) -> None:
        ids = set(self.circuit_ids())
        if ids - self._known_circuits:
            self._known_circuits |= ids
            async_dispatcher_send(self.hass, self.new_circuits_signal)

    @callback
    def mark_circuits_known(self, ids: set[int]) -> None:
        self._known_circuits |= ids

    @callback
    def _on_became_available(self) -> None:
        """The link is (back) up: re-read everything, like a reload but without one."""
        if self.read_only:
            self.schedule_sync.state = "listen_only"
            return

        @callback
        def start(_: Any) -> None:
            if self._resync_task and not self._resync_task.done():
                self._resync_again = True  # the running one may predate this reconnect
                return
            self._resync_task = self.config_entry.async_create_background_task(
                self.hass, self._resync_loop(), "easytouch_local resync"
            )

        # Give the link a moment to settle before asking for anything.
        self._unsubs.append(async_call_later(self.hass, RECONNECT_SETTLE, start))

    async def _resync_loop(self) -> None:
        reason = "link up"
        while True:
            self._resync_again = False
            await self.async_full_resync(reason)
            if not self._resync_again:
                return
            reason = "link up again"

    async def async_full_resync(self, reason: str) -> None:
        """Circuit setup, schedules and clock, in that order."""
        LOGGER.info("EasyTouch %s: re-reading circuits, schedules and clock", reason)
        try:
            await self.client.read_circuits()
        except EasyTouchError as err:
            LOGGER.warning("Couldn't read the circuit setup: %s", err)
        self._check_new_circuits()
        self.async_update_listeners()
        await self.async_reconcile_schedules(reason)
        await self.async_check_clock()

    # ------------------------------------------------------------------ #
    # Pool pump
    # ------------------------------------------------------------------ #
    def power_reading(self) -> float | None:
        """Live motor power (W) from the configured sensor, or None if unknown/stale."""
        entity_id = self.opt(CONF_POWER_ENTITY)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        reported = getattr(state, "last_reported", None) or state.last_updated
        if dt_util.utcnow() - reported > POWER_LIVE_WINDOW:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    @property
    def pump_relays_on(self) -> bool:
        """Every pump circuit is on."""
        status = self.client.status
        return bool(status and all(status.is_on(c) for c in self.pump_circuits))

    @property
    def pump_running(self) -> bool:
        """All pump circuits on; a live power reading can veto a motor that isn't turning."""
        if not self.pump_relays_on:
            return False
        watts = self.power_reading()
        if watts is None:
            return True
        settled = time.monotonic() - self._pump_changed_at > POWER_GRACE
        threshold = float(self.opt(CONF_POWER_THRESHOLD))
        return not (settled and watts < threshold)

    async def async_set_pump(self, on: bool) -> None:
        """Every pump circuit, in order on and in reverse off."""
        self._guard_command()
        circuits = self.pump_circuits if on else list(reversed(self.pump_circuits))
        try:
            for circuit in circuits:
                await self.client.set_circuit(circuit, on)
        except ReadOnlyError as err:
            raise HomeAssistantError(str(err)) from err
        except EasyTouchError as err:
            raise HomeAssistantError(f"Pool pump {'on' if on else 'off'} failed: {err}") from err
        finally:
            self._handle_client_event()

    async def async_set_circuit(self, circuit: int, on: bool) -> None:
        self._guard_command()
        try:
            await self.client.set_circuit(circuit, on)
        except EasyTouchError as err:
            raise HomeAssistantError(f"Circuit {circuit}: {err}") from err
        finally:
            self._handle_client_event()

    def _guard_command(self) -> None:
        if self.read_only:
            raise HomeAssistantError(
                "EasyTouch Local is in listen-only mode; turn that off in its options to control the pool"
            )
        if not self.client.available:
            raise HomeAssistantError("No live connection to the pool controller right now")
        status = self.client.status
        if status and status.service_mode:
            raise HomeAssistantError(
                "The pool panel is in Service/Timeout mode; finish at the panel first"
            )

    # ------------------------------------------------------------------ #
    # Schedules
    # ------------------------------------------------------------------ #
    def wanted_schedules(self) -> list[WantedSchedule]:
        """What the controller should hold right now."""
        if self.schedule_mode != SCHEDULE_MODE_CONTROLLER:
            return []  # HA runs the pump: the controller keeps no schedules
        enabled_entity = self.opt(CONF_SCHEDULE_ENABLED_ENTITY)
        if enabled_entity:
            state = self.hass.states.get(enabled_entity)
            if state is None:
                raise ValueError(f"{enabled_entity} doesn't exist")
            if state.state != STATE_ON:
                return []
        circuits = self.pump_circuits
        now = _now()
        if now.month in parse_months(self.opt(CONF_SUMMER_MONTHS)):
            windows = [
                (CONF_SUMMER_WEEKDAY_START, CONF_SUMMER_WEEKDAY_STOP, WEEKDAYS),
                (CONF_SUMMER_WEEKEND_START, CONF_SUMMER_WEEKEND_STOP, WEEKEND),
            ]
        else:
            windows = [(CONF_WINTER_START, CONF_WINTER_STOP, EVERY_DAY)]
        wanted: list[WantedSchedule] = []
        for start_key, stop_key, days in windows:
            start = self._helper_minutes(start_key)
            stop = self._helper_minutes(stop_key)
            if stop <= start:
                raise ValueError(
                    f"{self.opt(stop_key)} ({fmt_minutes(stop)}) isn't after "
                    f"{self.opt(start_key)} ({fmt_minutes(start)})"
                )
            wanted.extend(WantedSchedule(c, start, stop, days) for c in circuits)
        return wanted

    def _helper_minutes(self, key: str) -> int:
        entity_id = self.opt(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None:
            raise ValueError(f"time helper {entity_id or key} doesn't exist")
        try:
            t = dt_time.fromisoformat(state.state)
        except ValueError as err:
            raise ValueError(f"{entity_id} has no usable time ({state.state})") from err
        return t.hour * 60 + t.minute

    @callback
    def _schedule_reconcile(self, delay: float, reason: str) -> None:
        if self._schedule_timer:
            self._schedule_timer()

        @callback
        def run(_: Any) -> None:
            self._schedule_timer = None
            self.config_entry.async_create_background_task(
                self.hass, self.async_reconcile_schedules(reason), "easytouch_local schedules"
            )

        self._schedule_timer = async_call_later(self.hass, delay, run)

    @callback
    def _interval_schedule_check(self, _: datetime) -> None:
        self._schedule_reconcile(0, "periodic check")

    @callback
    def _helper_changed(self, _: Event[EventStateChangedData]) -> None:
        if not self.read_only:
            self._schedule_reconcile(SCHEDULE_DEBOUNCE, "schedule times changed")

    @callback
    def schedule_mode_changed(self) -> None:
        if not self.read_only:
            self._schedule_reconcile(1, "schedule mode changed")

    async def async_reconcile_schedules(self, reason: str) -> None:
        """Make the controller hold exactly the wanted schedules, verified by read-back."""
        sync = self.schedule_sync
        if self.read_only:
            sync.state = "listen_only"
            return
        if not self.client.available:
            return  # runs again when the link comes back
        status = self.client.status
        if status and status.service_mode:
            sync.state = "paused"
            self._schedule_reconcile(SCHEDULE_RETRY_DELAY, "service mode ended?")
            self.async_update_listeners()
            return
        if self._schedule_lock.locked():
            return
        async with self._schedule_lock:
            sync.state = "syncing"
            sync.last_attempt = dt_util.utcnow()
            self.async_update_listeners()
            try:
                wanted = self.wanted_schedules()
                current = await self.client.read_all_schedules()
                plan = plan_schedules(current, wanted)
                if not plan.empty:
                    LOGGER.info(
                        "Schedule sync (%s): clearing %s, writing %s",
                        reason,
                        plan.clears or "nothing",
                        [f"{s}: {w.describe()}" for s, w in plan.writes] or "nothing",
                    )
                for slot in plan.clears:
                    await self.client.write_schedule(slot, None)
                for slot, want in plan.writes:
                    await self.client.write_schedule(slot, want)
                if plan.unplaced:
                    raise EasyTouchError(
                        f"no free schedule slots for {', '.join(w.describe() for w in plan.unplaced)}"
                    )
                if not plan.empty:
                    final = await self.client.read_all_schedules()
                    if not plan_schedules(final, wanted).empty:
                        raise EasyTouchError("the controller's schedules still don't match")
            except (EasyTouchError, ValueError) as err:
                sync.state = "failed"
                sync.error = str(err)
                LOGGER.warning("Schedule sync failed (%s): %s", reason, err)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    SCHEDULE_ISSUE,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=SCHEDULE_ISSUE,
                    translation_placeholders={"error": str(err)},
                )
                self._schedule_reconcile(SCHEDULE_RETRY_DELAY, "retry after failure")
            else:
                sync.state = "in_sync"
                sync.error = None
                sync.last_success = dt_util.utcnow()
                ir.async_delete_issue(self.hass, DOMAIN, SCHEDULE_ISSUE)
            self.async_update_listeners()

    def schedule_summary(self) -> tuple[str, list[str]]:
        slots: list[ScheduleSlot] = [
            s for _, s in sorted(self.client.schedules.items()) if not s.empty and not s.egg_timer
        ]
        lines = [
            f"#{s.slot} {self.circuit_names.get(s.circuit, f'circuit {s.circuit}')} "
            f"{fmt_minutes(s.start)}-{fmt_minutes(s.end)} {fmt_days(s.days)}"
            for s in slots
        ]
        return (f"{len(lines)} schedule{'s' if len(lines) != 1 else ''}", lines)

    # ------------------------------------------------------------------ #
    # Clock
    # ------------------------------------------------------------------ #
    def _drift_minutes(self) -> int | None:
        status = self.client.status
        if status is None or not self.client.available:
            return None
        now = dt_util.now()
        diff = (status.hour * 60 + status.minute) - (now.hour * 60 + now.minute)
        return (diff + 720) % 1440 - 720

    @callback
    def _minute_tick(self, _: datetime) -> None:
        self.clock_drift = self._drift_minutes()
        if self.clock_drift is not None and abs(self.clock_drift) >= CLOCK_DRIFT_LIMIT:
            self._start_clock_job(force=False)

    @callback
    def _nightly_clock(self, _: datetime) -> None:
        self._start_clock_job(force=True)

    @callback
    def _start_clock_job(self, force: bool) -> None:
        """Clock sync may wait for the next minute, so it runs in the background."""
        if self._clock_task and not self._clock_task.done():
            return
        self._clock_task = self.config_entry.async_create_background_task(
            self.hass, self.async_check_clock(force=force), "easytouch_local clock"
        )

    async def async_check_clock(self, force: bool = False) -> None:
        if self.read_only or not self.client.available:
            return
        status = self.client.status
        if status and status.service_mode:
            return
        drift = self._drift_minutes()
        self.clock_drift = drift
        if not force:
            if drift is None or abs(drift) < CLOCK_DRIFT_LIMIT:
                return
            if time.monotonic() - self._last_clock_sync < CLOCK_MIN_RESYNC:
                return
        # Send just after a minute rolls over so the seconds line up.
        now = dt_util.now()
        if CLOCK_ALIGN and now.second > 5:
            await asyncio.sleep(60 - now.second + 1)
        try:
            await self.client.set_clock(dt_util.now())
        except EasyTouchError as err:
            LOGGER.warning("Couldn't set the pool controller clock: %s", err)
            return
        self._last_clock_sync = time.monotonic()
        LOGGER.info("Set the pool controller clock (drift was %s min)", drift)
        self.clock_drift = self._drift_minutes()
        self.async_update_listeners()

    # ------------------------------------------------------------------ #
    @callback
    def _power_changed(self, _: Event[EventStateChangedData]) -> None:
        self.async_update_listeners()
