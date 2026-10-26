"""Sensors: temperatures, chlorinator, panel mode, clock and schedules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EasyTouchConfigEntry
from .coordinator import EasyTouchCoordinator
from .entity import EasyTouchEntity

PANEL_MODES = ["auto", "service", "timeout", "service_timeout"]


def _temp_unit(c: EasyTouchCoordinator) -> str:
    s = c.client.status
    return UnitOfTemperature.CELSIUS if s and s.mode_byte & 0x04 else UnitOfTemperature.FAHRENHEIT


def _chlor_status(c: EasyTouchCoordinator) -> str | None:
    if c.client.chlor.status_byte is None:
        return None
    problems = c.client.chlor.problems
    return ", ".join(problems) if problems else "ok"


@dataclass(frozen=True, kw_only=True)
class EasyTouchSensorDescription(SensorEntityDescription):
    value: Callable[[EasyTouchCoordinator], Any]
    attrs: Callable[[EasyTouchCoordinator], dict[str, Any] | None] | None = None
    dynamic_temp_unit: bool = False
    always_available: bool = False


DESCRIPTIONS: tuple[EasyTouchSensorDescription, ...] = (
    EasyTouchSensorDescription(
        key="water_temp",
        translation_key="water_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        dynamic_temp_unit=True,
        value=lambda c: c.client.status.water_temp if c.client.status else None,
        attrs=lambda c: {"raw": c.client.status.water_raw} if c.client.status else None,
    ),
    EasyTouchSensorDescription(
        key="air_temp",
        translation_key="air_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        dynamic_temp_unit=True,
        value=lambda c: c.client.status.air_temp if c.client.status else None,
    ),
    EasyTouchSensorDescription(
        key="salt_level",
        translation_key="salt_level",
        native_unit_of_measurement="ppm",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:shaker-outline",
        value=lambda c: c.client.chlor.salt_ppm,
    ),
    EasyTouchSensorDescription(
        key="chlorinator_output",
        translation_key="chlorinator_output",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-percent",
        value=lambda c: c.client.chlor.output_pct,
    ),
    EasyTouchSensorDescription(
        key="chlorinator_status",
        translation_key="chlorinator_status",
        icon="mdi:water-alert",
        value=_chlor_status,
    ),
    EasyTouchSensorDescription(
        key="panel_mode",
        translation_key="panel_mode",
        device_class=SensorDeviceClass.ENUM,
        options=PANEL_MODES,
        icon="mdi:tablet-dashboard",
        value=lambda c: (
            c.client.status.panel_mode
            if c.client.status and c.client.status.panel_mode in PANEL_MODES
            else None
        ),
    ),
    EasyTouchSensorDescription(
        key="controller_schedules",
        translation_key="controller_schedules",
        icon="mdi:calendar-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        always_available=True,
        value=lambda c: c.schedule_summary()[0] if c.client.schedules else None,
        attrs=lambda c: {
            "schedules": c.schedule_summary()[1],
            "sync_state": c.schedule_sync.state,
            "sync_error": c.schedule_sync.error,
            "last_sync": c.schedule_sync.last_success,
            "mode": c.schedule_mode,
        },
    ),
    EasyTouchSensorDescription(
        key="controller_time",
        translation_key="controller_time",
        icon="mdi:clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda c: (
            f"{c.client.status.hour:02d}:{c.client.status.minute:02d}" if c.client.status else None
        ),
    ),
    EasyTouchSensorDescription(
        key="clock_drift",
        translation_key="clock_drift",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-alert-outline",
        value=lambda c: c.clock_drift,
    ),
    EasyTouchSensorDescription(
        key="delay_code",
        translation_key="delay_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda c: c.client.status.delay_byte if c.client.status else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EasyTouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(EasyTouchSensor(coordinator, d) for d in DESCRIPTIONS)


class EasyTouchSensor(EasyTouchEntity, SensorEntity):
    entity_description: EasyTouchSensorDescription

    def __init__(
        self, coordinator: EasyTouchCoordinator, description: EasyTouchSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self.entity_description.always_available or super().available

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.dynamic_temp_unit:
            return _temp_unit(self.coordinator)
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs is None:
            return None
        return self.entity_description.attrs(self.coordinator)
