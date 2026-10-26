"""Buttons: sync the controller clock and schedules now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EasyTouchConfigEntry
from .coordinator import EasyTouchCoordinator
from .entity import EasyTouchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EasyTouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    c = entry.runtime_data
    async_add_entities([SyncClockButton(c), SyncSchedulesButton(c)])


class _Button(EasyTouchEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def _check(self) -> None:
        if self.coordinator.read_only:
            raise HomeAssistantError("EasyTouch Local is in listen-only mode")


class SyncClockButton(_Button):
    _attr_translation_key = "sync_clock"
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, coordinator: EasyTouchCoordinator) -> None:
        super().__init__(coordinator, "sync_clock")

    async def async_press(self) -> None:
        self._check()
        await self.coordinator.async_check_clock(force=True)


class SyncSchedulesButton(_Button):
    _attr_translation_key = "sync_schedules"
    _attr_icon = "mdi:calendar-refresh"

    def __init__(self, coordinator: EasyTouchCoordinator) -> None:
        super().__init__(coordinator, "sync_schedules")

    async def async_press(self) -> None:
        self._check()
        await self.coordinator.async_reconcile_schedules("button")
        if self.coordinator.schedule_sync.state == "failed":
            raise HomeAssistantError(
                f"Schedule sync failed: {self.coordinator.schedule_sync.error}"
            )
