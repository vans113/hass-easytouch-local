"""Switches: the pool pump and every circuit on the controller."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EasyTouchConfigEntry
from .const import DOMAIN
from .coordinator import EasyTouchCoordinator
from .entity import EasyTouchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EasyTouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    added: set[int] = set()

    @callback
    def add_circuits() -> None:
        """Add a switch for every circuit we know about and haven't added yet."""
        _remove_missing(hass, coordinator)
        new = [c for c in coordinator.circuit_ids() if c not in added]
        added.update(new)
        coordinator.mark_circuits_known(set(new))
        if new:
            async_add_entities(CircuitSwitch(coordinator, c) for c in new)

    async_add_entities([PoolPumpSwitch(coordinator), SuperChlorinateSwitch(coordinator)])
    add_circuits()
    # Circuits found later (circuit setup read after a reconnect) appear on the fly.
    entry.async_on_unload(
        async_dispatcher_connect(hass, coordinator.new_circuits_signal, add_circuits)
    )


@callback
def _remove_missing(hass: HomeAssistant, coordinator: EasyTouchCoordinator) -> None:
    """Drop switches for circuits this panel model doesn't have.

    Older versions created them before the model was known.
    """
    registry = er.async_get(hass)
    for circuit in coordinator.missing_circuits():
        unique_id = f"{coordinator.unique_id}_circuit_{circuit}"
        if entity_id := registry.async_get_entity_id("switch", DOMAIN, unique_id):
            registry.async_remove(entity_id)


class PoolPumpSwitch(EasyTouchEntity, SwitchEntity):
    """Turns the configured pump circuits on together (and off in reverse).

    Some pools need more than one circuit for the pump to keep running, for
    example Pool plus a two-speed pump's High Speed. It's on when all of them
    are on and, if a live power reading is configured, the motor is drawing
    power.
    """

    _attr_translation_key = "pool_pump"
    _attr_icon = "mdi:pump"

    def __init__(self, coordinator: EasyTouchCoordinator) -> None:
        super().__init__(coordinator, "pool_pump")

    @property
    def is_on(self) -> bool:
        return self.coordinator.pump_running

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator
        status = self.status
        names = [c.circuit_name(cid) for cid in c.pump_circuits]
        return {
            # Plain-language summary of what this switch does on this pool.
            "controls": " + ".join(names),
            "description": (
                f"Turns on {' then '.join(names)} together and off in reverse. "
                "Set in the integration options under Pump circuits."
                if len(names) > 1
                else f"Turns {names[0]} on and off. Set in the integration options "
                "under Pump circuits."
            ),
            "circuits": {
                c.circuit_name(cid): bool(status and status.is_on(cid)) for cid in c.pump_circuits
            },
            "relays_on": c.pump_relays_on,
            "motor_power_w": c.power_reading(),
            "high_speed_active": bool(status and status.high_speed_on),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_pump(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_pump(False)


class CircuitSwitch(EasyTouchEntity, SwitchEntity):
    """One controller circuit, named as on the panel."""

    _attr_icon = "mdi:electric-switch"

    def __init__(self, coordinator: EasyTouchCoordinator, circuit: int) -> None:
        super().__init__(coordinator, f"circuit_{circuit}")
        self.circuit = circuit

    @property
    def name(self) -> str:
        return self.coordinator.circuit_name(self.circuit)

    @property
    def is_on(self) -> bool:
        status = self.status
        return bool(status and status.is_on(self.circuit))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self.coordinator.client.circuits.get(self.circuit)
        return {
            "circuit_id": self.circuit,
            "pump_circuit": self.circuit in self.coordinator.pump_circuits,
            "freeze_protect": info.freeze if info else None,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_circuit(self.circuit, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_circuit(self.circuit, False)


class SuperChlorinateSwitch(EasyTouchEntity, SwitchEntity):
    """IntelliChlor super-chlorinate, run by the controller.

    On starts it for the hours set in the options (the EasyTouch keeps the
    pump on for that long); off cancels it. Read back from the controller.
    """

    _attr_translation_key = "super_chlorinate"
    _attr_icon = "mdi:flash-alert"

    def __init__(self, coordinator: EasyTouchCoordinator) -> None:
        super().__init__(coordinator, "super_chlorinate")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.client.chlor.present

    @property
    def is_on(self) -> bool:
        return self.coordinator.client.chlor.super_chlor_hours > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        chlor = self.coordinator.client.chlor
        return {
            "hours_left": chlor.super_chlor_hours,
            "pool_setpoint_pct": chlor.pool_setpoint,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_super_chlorinate(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_super_chlorinate(False)
