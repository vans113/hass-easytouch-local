"""EasyTouch Local: native control of a Pentair EasyTouch over an RS-485 bridge."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .client import EasyTouchClient
from .const import (
    CONF_READ_ONLY,
    CONF_SOURCE_ADDR,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOGGER,
    MANUFACTURER,
    MODEL,
    SETUP_WAIT,
)
from .coordinator import EasyTouchCoordinator

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type EasyTouchConfigEntry = ConfigEntry[EasyTouchCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EasyTouchConfigEntry) -> bool:
    """Start the link and set up entities.

    Setup never fails because the bridge is down: the client keeps trying on
    its own and entities stay unavailable until status arrives.
    """
    options = {**DEFAULT_OPTIONS, **entry.options}
    client = EasyTouchClient(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        source_addr=int(options[CONF_SOURCE_ADDR]),
        read_only=bool(options[CONF_READ_ONLY]),
    )
    client.start()
    if not await client.wait_for_status(SETUP_WAIT):
        LOGGER.warning(
            "No status from the EasyTouch at %s:%s yet; it will keep trying",
            client.host,
            client.port,
        )
    coordinator = EasyTouchCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    await coordinator.async_refresh()

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, coordinator.unique_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model=MODEL,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_jobs()
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: EasyTouchConfigEntry) -> None:
    """Reload when options change, except for the schedule-mode select."""
    coordinator = entry.runtime_data
    if getattr(coordinator, "suppress_reload", False):
        coordinator.suppress_reload = False
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EasyTouchConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
