"""EasyTouch Local: native control of a Pentair EasyTouch over an RS-485 bridge."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .client import EasyTouchClient
from .const import (
    CONF_BRIDGE_HOST,
    CONF_CONNECTION,
    CONF_MANAGE_SCHEDULES,
    CONF_READ_ONLY,
    CONF_SOURCE_ADDR,
    CONNECTION_LISTEN,
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
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type EasyTouchConfigEntry = ConfigEntry[EasyTouchCoordinator]

BRIDGE_ADDRESS_ISSUE = "bridge_address_missing"


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.1 -> 1.2: schedule management became an explicit option.

    Entries that were already sending to the controller were already having
    their pump schedules managed; they keep that (now limited to the pump
    circuits). Listen-only entries start with it off.
    """
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        options = dict(entry.options)
        options.setdefault(
            CONF_MANAGE_SCHEDULES, not options.get(CONF_READ_ONLY, DEFAULT_OPTIONS[CONF_READ_ONLY])
        )
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EasyTouchConfigEntry) -> bool:
    """Start the link and set up entities.

    Setup never fails because the bridge is down: the client keeps trying on
    its own and entities stay unavailable until status arrives.
    """
    options = {**DEFAULT_OPTIONS, **entry.options}
    listen = entry.data.get(CONF_CONNECTION) == CONNECTION_LISTEN
    bridge_host = entry.data.get(CONF_BRIDGE_HOST) or None
    if listen and bridge_host is None:
        # Set up before the bridge address was asked for: still accepts any
        # caller until the user adds it with Reconfigure.
        LOGGER.warning(
            "EasyTouch Local is listening on port %s without a bridge address, so any host "
            "can connect; set the bridge address with Reconfigure",
            entry.data[CONF_PORT],
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{BRIDGE_ADDRESS_ISSUE}_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=BRIDGE_ADDRESS_ISSUE,
            translation_placeholders={"port": str(entry.data[CONF_PORT])},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, f"{BRIDGE_ADDRESS_ISSUE}_{entry.entry_id}")
    client = EasyTouchClient(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        source_addr=int(options[CONF_SOURCE_ADDR]),
        read_only=bool(options[CONF_READ_ONLY]),
        listen=listen,
        allowed_peer=bridge_host if listen else None,
    )
    client.start()
    if not await client.wait_for_status(SETUP_WAIT):
        LOGGER.warning(
            "No status from the EasyTouch yet (%s %s:%s); it will keep trying",
            "listening on" if client.listen else "bridge",
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


async def async_remove_entry(hass: HomeAssistant, entry: EasyTouchConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, f"{BRIDGE_ADDRESS_ISSUE}_{entry.entry_id}")
