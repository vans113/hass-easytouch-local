"""Base entity for EasyTouch Local."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import EasyTouchCoordinator


class EasyTouchEntity(CoordinatorEntity[EasyTouchCoordinator]):
    """Anything that belongs to the controller."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EasyTouchCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.unique_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def available(self) -> bool:
        # Only trust fresh controller status; stale values are worse than none.
        return self.coordinator.client.available

    @property
    def status(self):
        return self.coordinator.client.status
