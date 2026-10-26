"""Number: the chlorinator's pool output setpoint."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
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
    async_add_entities([ChlorinatorOutputNumber(entry.runtime_data)])


class ChlorinatorOutputNumber(EasyTouchEntity, NumberEntity):
    """How hard the IntelliChlor runs while the pump is on (the panel's pool %).

    Changing it leaves a running super-chlorinate alone. Confirmed by reading
    the setting back from the controller.
    """

    _attr_translation_key = "chlorinator_setpoint"
    _attr_icon = "mdi:water-percent"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: EasyTouchCoordinator) -> None:
        super().__init__(coordinator, "chlorinator_setpoint")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.client.chlor.present

    @property
    def native_value(self) -> float | None:
        return self.coordinator.client.chlor.pool_setpoint

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_chlor_output(round(value))
