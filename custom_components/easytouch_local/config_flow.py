"""Config and options flows for EasyTouch Local."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from .client import EasyTouchClient
from .const import (
    CONF_EXTRA_CIRCUITS,
    CONF_HIGH_SPEED_CIRCUIT,
    CONF_POOL_CIRCUIT,
    CONF_POWER_ENTITY,
    CONF_POWER_THRESHOLD,
    CONF_READ_ONLY,
    CONF_SCHEDULE_ENABLED_ENTITY,
    CONF_SCHEDULE_MODE,
    CONF_SOURCE_ADDR,
    CONF_SUMMER_MONTHS,
    DEFAULT_OPTIONS,
    DEFAULT_PORT,
    DOMAIN,
    LOGGER,
    SCHEDULE_MODES,
    TIME_HELPER_KEYS,
)
from .coordinator import parse_months

PROBE_WAIT = 10.0


async def _probe(host: str, port: int) -> bool:
    """Listen (never send) until a status broadcast arrives."""
    client = EasyTouchClient(host, port, read_only=True)
    client.start()
    try:
        return await client.wait_for_status(PROBE_WAIT)
    finally:
        await client.stop()


def _connection_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
        }
    )


class EasyTouchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add the controller by its RS-485 bridge address."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EasyTouchOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            try:
                ok = await _probe(host, port)
            except Exception:
                LOGGER.exception("Unexpected error probing %s:%s", host, port)
                errors["base"] = "unknown"
            else:
                if ok:
                    return self.async_create_entry(
                        title="Pool controller",
                        data={CONF_HOST: host, CONF_PORT: port},
                        options={
                            **DEFAULT_OPTIONS,
                            CONF_READ_ONLY: user_input.get(CONF_READ_ONLY, True),
                        },
                    )
                errors["base"] = "no_status"
        schema = _connection_schema(user_input or {}).extend(
            {vol.Optional(CONF_READ_ONLY, default=True): bool}
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the bridge address."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            if await _probe(host, port):
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=f"{host}:{port}",
                    data_updates={CONF_HOST: host, CONF_PORT: port},
                )
            errors["base"] = "no_status"
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(user_input or dict(entry.data)),
            errors=errors,
        )


class EasyTouchOptionsFlow(OptionsFlow):
    """Everything about how the pool is wired and scheduled."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        if user_input is not None:
            try:
                if not parse_months(user_input[CONF_SUMMER_MONTHS]):
                    raise ValueError
            except ValueError:
                errors[CONF_SUMMER_MONTHS] = "bad_months"
            if not errors:
                cleaned = {k: (v or "") if isinstance(v, str) else v for k, v in user_input.items()}
                # A cleared optional field is simply missing from the form data.
                for key in (CONF_POWER_ENTITY, CONF_SCHEDULE_ENABLED_ENTITY, *TIME_HELPER_KEYS):
                    cleaned.setdefault(key, "")
                return self.async_create_entry(data={**current, **cleaned})

        entity = selector.EntitySelector
        schema = vol.Schema(
            {
                vol.Required(CONF_READ_ONLY, default=current[CONF_READ_ONLY]): bool,
                vol.Required(
                    CONF_SCHEDULE_MODE, default=current[CONF_SCHEDULE_MODE]
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=SCHEDULE_MODES, translation_key="schedule_mode"
                    )
                ),
                vol.Required(CONF_POOL_CIRCUIT, default=current[CONF_POOL_CIRCUIT]): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=40)
                ),
                vol.Required(
                    CONF_HIGH_SPEED_CIRCUIT, default=current[CONF_HIGH_SPEED_CIRCUIT]
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=40)),
                vol.Optional(CONF_EXTRA_CIRCUITS, default=current[CONF_EXTRA_CIRCUITS]): str,
                vol.Optional(
                    CONF_POWER_ENTITY,
                    description={"suggested_value": current[CONF_POWER_ENTITY] or None},
                ): entity(selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(CONF_POWER_THRESHOLD, default=current[CONF_POWER_THRESHOLD]): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=5000)
                ),
                vol.Required(CONF_SUMMER_MONTHS, default=current[CONF_SUMMER_MONTHS]): str,
                vol.Optional(
                    CONF_SCHEDULE_ENABLED_ENTITY,
                    description={"suggested_value": current[CONF_SCHEDULE_ENABLED_ENTITY] or None},
                ): entity(selector.EntitySelectorConfig(domain=["input_boolean", "switch"])),
                **{
                    vol.Optional(
                        key, description={"suggested_value": current[key] or None}
                    ): entity(selector.EntitySelectorConfig(domain=["input_datetime", "time"]))
                    for key in TIME_HELPER_KEYS
                },
                vol.Required(CONF_SOURCE_ADDR, default=current[CONF_SOURCE_ADDR]): vol.All(
                    vol.Coerce(int), vol.Range(min=32, max=47)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
