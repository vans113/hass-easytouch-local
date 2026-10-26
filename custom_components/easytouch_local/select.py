"""Select: who runs the pump schedule (Home Assistant or the controller)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EasyTouchConfigEntry
from .const import CONF_SCHEDULE_MODE, SCHEDULE_MODES
from .coordinator import EasyTouchCoordinator
from .entity import EasyTouchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EasyTouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ScheduleModeSelect(entry.runtime_data)])


class ScheduleModeSelect(EasyTouchEntity, SelectEntity):
    """home_assistant: HA turns the pump on/off and the controller keeps no schedules.
    controller: HA writes the schedules into the controller, which runs the pump itself.
    """

    _attr_translation_key = "schedule_mode"
    _attr_options = SCHEDULE_MODES
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-sync"

    def __init__(self, coordinator: EasyTouchCoordinator) -> None:
        super().__init__(coordinator, "schedule_mode")

    @property
    def available(self) -> bool:
        return True  # a setting, not a reading

    @property
    def current_option(self) -> str:
        return self.coordinator.schedule_mode

    async def async_select_option(self, option: str) -> None:
        entry = self.coordinator.config_entry
        self.coordinator.suppress_reload = True
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_SCHEDULE_MODE: option}
        )
        self.coordinator.schedule_mode_changed()
        self.async_write_ha_state()
