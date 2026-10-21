"""Config flow for authenticated SmartHome Assistant Core access."""
from __future__ import annotations

from typing import Any

import probatio
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    CoreAuthError,
    CoreClient,
    CoreError,
    CoreProtocolError,
    normalize_core_url,
)
from .const import (
    CONF_BINDINGS,
    CONF_CORE_URL,
    CONF_TIMER_OUTPUTS,
    CONF_TOKEN,
    DOMAIN,
    validate_timer_outputs,
)


def _validate_bindings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("Bindings must be an object")
    result = {}
    for key, target in value.items():
        if (not isinstance(key, str) or not key.strip()
                or not isinstance(target, str) or not target.strip()):
            raise ValueError("Each binding needs a device or satellite ID and a gateway satellite ID")
        result[key.strip()] = target.strip()
    return result


async def _validate_core(hass, url: str, token: str) -> str:
    normalized = normalize_core_url(url)
    if not token.strip():
        raise CoreAuthError("A bearer token is required")
    client = CoreClient(async_get_clientsession(hass), normalized, token.strip(), timeout=10)
    info = await client.async_get_info()
    client.validate_info(info)
    return normalized


async def _validate_timer_outputs(hass, value):
    outputs = validate_timer_outputs(value)
    manager = getattr(hass, "data", {}).get("smarthome_jobs")
    if manager is not None:
        await manager.async_validate_output_bindings(outputs)
    return outputs


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            try:
                timer_outputs = await _validate_timer_outputs(self.hass,
                    user_input.get(CONF_TIMER_OUTPUTS, {}),
                )
            except ValueError:
                errors["base"] = "invalid_timer_outputs"
                return self.async_show_form(
                    step_id="user", data_schema=self._user_schema(), errors=errors,
                )

            try:
                core_url = await _validate_core(
                    self.hass, user_input[CONF_CORE_URL], user_input[CONF_TOKEN],
                )
                bindings = _validate_bindings(user_input.get(CONF_BINDINGS, {}))
            except CoreAuthError:
                errors["base"] = "invalid_auth"
            except CoreProtocolError:
                errors["base"] = "unsupported_protocol"
            except (CoreError, ValueError, KeyError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="SmartHome Assistant",
                    data={CONF_CORE_URL: core_url, CONF_TOKEN: user_input[CONF_TOKEN].strip(),
                          CONF_BINDINGS: bindings, CONF_TIMER_OUTPUTS: timer_outputs},
                )

        return self.async_show_form(step_id="user", data_schema=self._user_schema(), errors=errors)

    @staticmethod
    def _user_schema():
        return probatio.Schema({
            probatio.Required(CONF_CORE_URL): selector.TextSelector(),
            probatio.Required(CONF_TOKEN): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
            ),
            probatio.Optional(CONF_BINDINGS, default=dict): selector.ObjectSelector(),
            probatio.Optional(CONF_TIMER_OUTPUTS, default=dict): selector.ObjectSelector(),
        })

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            try:
                timer_outputs = await _validate_timer_outputs(self.hass, user_input.get(
                    CONF_TIMER_OUTPUTS,
                    entry.options.get(CONF_TIMER_OUTPUTS, entry.data.get(CONF_TIMER_OUTPUTS, {})),
                ))
            except ValueError:
                errors["base"] = "invalid_timer_outputs"
                return self.async_show_form(
                    step_id="reconfigure", data_schema=self._reconfigure_schema(entry), errors=errors,
                )

            try:
                core_url = await _validate_core(
                    self.hass, user_input[CONF_CORE_URL], user_input[CONF_TOKEN],
                )
                bindings = _validate_bindings(user_input.get(
                    CONF_BINDINGS, entry.options.get(CONF_BINDINGS, entry.data.get(CONF_BINDINGS, {})),
                ))
            except CoreAuthError:
                errors["base"] = "invalid_auth"
            except CoreProtocolError:
                errors["base"] = "unsupported_protocol"
            except (CoreError, ValueError, KeyError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_CORE_URL: core_url, CONF_TOKEN: user_input[CONF_TOKEN].strip(),
                                  CONF_BINDINGS: bindings, CONF_TIMER_OUTPUTS: timer_outputs},
                    options={CONF_BINDINGS: bindings, CONF_TIMER_OUTPUTS: timer_outputs},
                    reason="reconfigure_successful",
                )

        return self.async_show_form(
            step_id="reconfigure", data_schema=self._reconfigure_schema(entry), errors=errors,
        )

    @staticmethod
    def _reconfigure_schema(entry):
        return probatio.Schema({
            probatio.Required(CONF_CORE_URL, default=entry.data[CONF_CORE_URL]): selector.TextSelector(),
            probatio.Required(CONF_TOKEN, default=entry.data[CONF_TOKEN]): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
            ),
            probatio.Optional(CONF_BINDINGS, default=entry.options.get(
                CONF_BINDINGS, entry.data.get(CONF_BINDINGS, {}),
            )): selector.ObjectSelector(),
            probatio.Optional(CONF_TIMER_OUTPUTS, default=entry.options.get(
                CONF_TIMER_OUTPUTS, entry.data.get(CONF_TIMER_OUTPUTS, {}),
            )): selector.ObjectSelector(),
        })

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            try:
                timer_outputs = await _validate_timer_outputs(self.hass, user_input.get(
                    CONF_TIMER_OUTPUTS,
                    self.config_entry.options.get(
                        CONF_TIMER_OUTPUTS, self.config_entry.data.get(CONF_TIMER_OUTPUTS, {}),
                    ),
                ))
            except ValueError:
                errors["base"] = "invalid_timer_outputs"
                return self.async_show_form(step_id="init", data_schema=self._options_schema(), errors=errors)

            try:
                bindings = _validate_bindings(user_input.get(
                    CONF_BINDINGS,
                    self.config_entry.options.get(
                        CONF_BINDINGS, self.config_entry.data.get(CONF_BINDINGS, {}),
                    ),
                ))
            except ValueError:
                errors["base"] = "invalid_bindings"
            else:
                return self.async_create_entry(
                    title="", data={CONF_BINDINGS: bindings, CONF_TIMER_OUTPUTS: timer_outputs},
                )

        return self.async_show_form(step_id="init", data_schema=self._options_schema(), errors=errors)

    def _options_schema(self):
        current = self.config_entry.options.get(
            CONF_BINDINGS, self.config_entry.data.get(CONF_BINDINGS, {}),
        )
        current_timer_outputs = self.config_entry.options.get(
            CONF_TIMER_OUTPUTS, self.config_entry.data.get(CONF_TIMER_OUTPUTS, {}),
        )
        return probatio.Schema({
            probatio.Optional(CONF_BINDINGS, default=current): selector.ObjectSelector(),
            probatio.Optional(CONF_TIMER_OUTPUTS, default=current_timer_outputs): selector.ObjectSelector(),
        })
