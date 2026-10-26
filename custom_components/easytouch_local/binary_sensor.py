"""Binary sensors: link health, panel state, start timer, faults."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EasyTouchConfigEntry
from .coordinator import EasyTouchCoordinator
from .entity import EasyTouchEntity


@dataclass(frozen=True, kw_only=True)
class EasyTouchBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[EasyTouchCoordinator], bool | None]
    always_available: bool = False


DESCRIPTIONS: tuple[EasyTouchBinaryDescription, ...] = (
    EasyTouchBinaryDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda c: c.client.available,
        always_available=True,
    ),
    EasyTouchBinaryDescription(
        key="service_mode",
        translation_key="service_mode",
        icon="mdi:account-wrench",
        value=lambda c: c.client.status.service_mode if c.client.status else None,
    ),
    EasyTouchBinaryDescription(
        key="freeze_protect",
        translation_key="freeze_protect",
        icon="mdi:snowflake-alert",
        value=lambda c: c.client.status.freeze_protect if c.client.status else None,
    ),
    EasyTouchBinaryDescription(
        key="start_timer",
        translation_key="start_timer",
        icon="mdi:timer-play-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda c: c.client.status.start_timer if c.client.status else None,
    ),
    EasyTouchBinaryDescription(
        key="water_sensor_fault",
        translation_key="water_sensor_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value=lambda c: c.client.status.water_sensor_fault if c.client.status else None,
    ),
    EasyTouchBinaryDescription(
        key="schedule_problem",
        translation_key="schedule_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value=lambda c: c.schedule_sync.state == "failed",
        always_available=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EasyTouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(EasyTouchBinarySensor(coordinator, d) for d in DESCRIPTIONS)


class EasyTouchBinarySensor(EasyTouchEntity, BinarySensorEntity):
    entity_description: EasyTouchBinaryDescription

    def __init__(
        self, coordinator: EasyTouchCoordinator, description: EasyTouchBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self.entity_description.always_available or super().available

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict | None:
        c = self.coordinator
        if self.entity_description.key == "schedule_problem":
            return {"sync_state": c.schedule_sync.state, "error": c.schedule_sync.error}
        if self.entity_description.key == "connected":
            st = c.client.stats
            return {
                "watchdog_trips": st.watchdog_trips,
                "reconnects": st.reconnects,
                "last_error": st.last_error,
                "listen_only": c.read_only,
            }
        return None
