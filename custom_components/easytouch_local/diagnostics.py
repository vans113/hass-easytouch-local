"""Diagnostics download for EasyTouch Local."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import EasyTouchConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EasyTouchConfigEntry
) -> dict[str, Any]:
    c = entry.runtime_data
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "link": c.client.diagnostics(),
        "schedule_sync": {
            "state": c.schedule_sync.state,
            "error": c.schedule_sync.error,
            "last_success": str(c.schedule_sync.last_success),
        },
        "clock_drift_min": c.clock_drift,
        "panel_model": str(c.client.status.model) if c.client.status else None,
        "missing_circuits": sorted(c.missing_circuits()),
        "pump": {
            "running": c.pump_running,
            "pump_circuits": c.pump_circuits,
            "pump_relays_on": c.pump_relays_on,
            "power_w": c.power_reading(),
        },
    }
