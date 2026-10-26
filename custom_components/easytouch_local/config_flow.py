"""Config and options flows for EasyTouch Local."""

from __future__ import annotations

import asyncio
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
    CONF_CIRCUIT_NAMES,
    CONF_CONNECTION,
    CONF_POWER_ENTITY,
    CONF_POWER_THRESHOLD,
    CONF_PUMP_CIRCUITS,
    CONF_READ_ONLY,
    CONF_SCHEDULE_ENABLED_ENTITY,
    CONF_SCHEDULE_MODE,
    CONF_SOURCE_ADDR,
    CONF_SUMMER_MONTHS,
    CONF_SUPER_CHLOR_HOURS,
    CONNECTION_CONNECT,
    CONNECTION_LISTEN,
    DEFAULT_OPTIONS,
    DEFAULT_PORT,
    DOMAIN,
    LISTEN_HOST,
    LOGGER,
    PROBE_WAIT_LISTEN,
    SCHEDULE_MODES,
    TIME_HELPER_KEYS,
)
from .coordinator import parse_circuit_list, parse_months

PROBE_WAIT = 10.0

PORT_FIELD = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))


async def _check_port_free(host: str, port: int) -> None:
    """Raise OSError if something else already listens on the port."""
    server = await asyncio.start_server(lambda r, w: w.close(), host, port, reuse_address=True)
    server.close()
    await server.wait_closed()


async def _probe(host: str, port: int, listen: bool) -> bool:
    """Listen (never send) until a status broadcast arrives."""
    if listen:
        await _check_port_free(host, port)  # fail fast if the port is taken
    client = EasyTouchClient(host, port, read_only=True, listen=listen)
    client.start()
    try:
        return await client.wait_for_status(PROBE_WAIT_LISTEN if listen else PROBE_WAIT)
    finally:
        await client.stop()


class EasyTouchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add the controller by its RS-485 bridge."""

    VERSION = 1

    def __init__(self) -> None:
        self._read_only = True

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EasyTouchOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Which side opens the connection."""
        return self.async_show_menu(step_id="user", menu_options=["connect", "listen"])

    async def async_step_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The bridge is a TCP server; Home Assistant connects to it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            return await self._finish(CONNECTION_CONNECT, host, port, user_input, errors)
        return self.async_show_form(
            step_id="connect",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): PORT_FIELD,
                    vol.Optional(CONF_READ_ONLY, default=True): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_listen(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The bridge is a TCP client; it connects to Home Assistant on this port."""
        errors: dict[str, str] = {}
        if user_input is not None:
            port = int(user_input[CONF_PORT])
            result = await self._finish(CONNECTION_LISTEN, LISTEN_HOST, port, user_input, errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="listen",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): PORT_FIELD,
                    vol.Optional(CONF_READ_ONLY, default=True): bool,
                }
            ),
            errors=errors,
        )

    async def _finish(
        self,
        connection: str,
        host: str,
        port: int,
        user_input: dict[str, Any],
        errors: dict[str, str],
    ) -> ConfigFlowResult:
        unique = f"listen:{port}" if connection == CONNECTION_LISTEN else f"{host}:{port}"
        await self.async_set_unique_id(unique)
        self._abort_if_unique_id_configured()
        try:
            if connection == CONNECTION_LISTEN:
                # The bridge may not be pointed here yet; just make sure the port
                # is free. Entities stay unavailable until it dials in.
                await _check_port_free(host, port)
                ok = True
            else:
                ok = await _probe(host, port, False)
        except OSError:
            errors["base"] = "port_in_use"
            ok = False
        except Exception:
            LOGGER.exception("Unexpected error probing %s:%s", host, port)
            errors["base"] = "unknown"
            ok = False
        if ok:
            return self.async_create_entry(
                title="Pool controller",
                data={CONF_CONNECTION: connection, CONF_HOST: host, CONF_PORT: port},
                options={**DEFAULT_OPTIONS, CONF_READ_ONLY: user_input.get(CONF_READ_ONLY, True)},
            )
        errors.setdefault("base", "no_status")
        return self.async_show_form(
            step_id=connection,
            data_schema=self._schema_for(connection, user_input),
            errors=errors,
        )

    @staticmethod
    def _schema_for(connection: str, user_input: dict[str, Any]) -> vol.Schema:
        fields: dict = {}
        if connection == CONNECTION_CONNECT:
            fields[vol.Required(CONF_HOST, default=user_input.get(CONF_HOST, ""))] = str
        fields[vol.Required(CONF_PORT, default=user_input.get(CONF_PORT, DEFAULT_PORT))] = (
            PORT_FIELD
        )
        fields[vol.Optional(CONF_READ_ONLY, default=user_input.get(CONF_READ_ONLY, True))] = bool
        return vol.Schema(fields)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the bridge address or listening port."""
        entry = self._get_reconfigure_entry()
        listen = entry.data.get(CONF_CONNECTION) == CONNECTION_LISTEN
        errors: dict[str, str] = {}
        if user_input is not None:
            host = LISTEN_HOST if listen else user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            # Free the port/bridge slot so the probe can get in.
            await entry.runtime_data.client.stop()
            if await _probe(host, port, listen):
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=f"listen:{port}" if listen else f"{host}:{port}",
                    data_updates={CONF_HOST: host, CONF_PORT: port},
                )
            entry.runtime_data.client.start()
            errors["base"] = "no_status"
        current = user_input or dict(entry.data)
        fields: dict = {}
        if not listen:
            fields[vol.Required(CONF_HOST, default=current.get(CONF_HOST, ""))] = str
        fields[vol.Required(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT))] = PORT_FIELD
        return self.async_show_form(
            step_id="reconfigure", data_schema=vol.Schema(fields), errors=errors
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
            if not parse_circuit_list(user_input[CONF_PUMP_CIRCUITS]):
                errors[CONF_PUMP_CIRCUITS] = "bad_circuits"
            if not errors:
                cleaned = {k: (v or "") if isinstance(v, str) else v for k, v in user_input.items()}
                # A cleared optional field is simply missing from the form data.
                for key in (
                    CONF_POWER_ENTITY,
                    CONF_SCHEDULE_ENABLED_ENTITY,
                    CONF_CIRCUIT_NAMES,
                    *TIME_HELPER_KEYS,
                ):
                    cleaned.setdefault(key, "")
                return self.async_create_entry(data={**current, **cleaned})

        coordinator = getattr(self.config_entry, "runtime_data", None)
        found = (
            ", ".join(f"{c}: {n}" for c, n in coordinator.circuit_names.items())
            if coordinator is not None
            else ""
        ) or "none yet"
        pump_now = (
            " + ".join(
                f"{coordinator.circuit_name(cid)} ({cid})" for cid in coordinator.pump_circuits
            )
            if coordinator is not None
            else current[CONF_PUMP_CIRCUITS]
        )
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
                vol.Required(CONF_PUMP_CIRCUITS, default=current[CONF_PUMP_CIRCUITS]): str,
                vol.Optional(
                    CONF_CIRCUIT_NAMES,
                    description={"suggested_value": current[CONF_CIRCUIT_NAMES] or None},
                ): str,
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
                vol.Required(
                    CONF_SUPER_CHLOR_HOURS, default=current[CONF_SUPER_CHLOR_HOURS]
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=96)),
                vol.Required(CONF_SOURCE_ADDR, default=current[CONF_SOURCE_ADDR]): vol.All(
                    vol.Coerce(int), vol.Range(min=32, max=47)
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"circuits": found, "pump": pump_now},
        )
