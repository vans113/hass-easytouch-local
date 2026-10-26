"""Switches: the pool pump and the controller's circuits."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EasyTouchConfigEntry
from .coordinator import EasyTouchCoordinator
from .entity import EasyTouchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EasyTouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [PoolPumpSwitch(coordinator)]
    entities += [
        CircuitSwitch(coordinator, circuit, name)
        for circuit, name in sorted(coordinator.circuit_names.items())
    ]
    async_add_entities(entities)


class PoolPumpSwitch(EasyTouchEntity, SwitchEntity):
    """Pool + high speed together; on only when the motor is really running.

    The motor runs on the high-speed relay. The Pool circuit alone only holds
    that relay for the controller's 5-minute start timer, so both go together.
    When a live power reading is configured, a relay that's on with no motor
    draw (after a grace period) shows as off.
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
        return {
            "high_speed_relay": c.high_speed_on,
            "pool_circuit": bool(status and status.is_on(c.pool_circuit)),
            "motor_power_w": c.power_reading(),
            "start_timer": bool(status and status.start_timer),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_pump(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_pump(False)


class CircuitSwitch(EasyTouchEntity, SwitchEntity):
    """One controller circuit, as the controller reports it."""

    def __init__(self, coordinator: EasyTouchCoordinator, circuit: int, name: str) -> None:
        super().__init__(coordinator, f"circuit_{circuit}")
        self.circuit = circuit
        self._attr_name = name
        self._attr_icon = "mdi:electric-switch"

    @property
    def is_on(self) -> bool:
        status = self.status
        return bool(status and status.is_on(self.circuit))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"circuit_id": self.circuit}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_circuit(self.circuit, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_circuit(self.circuit, False)
