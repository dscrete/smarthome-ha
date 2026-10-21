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
    CONF_CORE_URL,
    CONF_PROFILE_ID,
    CONF_TIMER_OUTPUTS,
    CONF_TOKEN,
    DOMAIN,
    validate_timer_outputs,
)


def _profile_choices(info: dict[str, Any], retained_profile_id: str | None = None):
    """Build selector options, retaining a saved ID even if Core no longer lists it."""
    choices = [{"value": "", "label": "Use Core default"}]
    profile_ids = set()
    profiles = info.get("profiles")
    if isinstance(profiles, list):
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            profile_id = profile.get("id")
            name = profile.get("name")
            if (not isinstance(profile_id, str) or not profile_id.strip()
                    or not isinstance(name, str) or not name.strip()):
                continue
            profile_id = profile_id.strip()
            if profile_id in profile_ids:
                continue
            profile_ids.add(profile_id)
            choices.append({"value": profile_id, "label": name.strip()})

    if isinstance(retained_profile_id, str) and retained_profile_id.strip():
        profile_id = retained_profile_id.strip()
        if profile_id not in profile_ids:
            choices.append({
                "value": profile_id,
                "label": f"{profile_id} (saved; not available in Core)",
            })
    return choices


def _persona_selector(choices):
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=choices,
            mode=selector.SelectSelectorMode.DROPDOWN,
        ),
    )


def _current_profile_id(entry) -> str:
    profile_id = entry.options.get(
        CONF_PROFILE_ID, entry.data.get(CONF_PROFILE_ID, ""),
    )
    return profile_id.strip() if isinstance(profile_id, str) else ""


async def _validate_core(hass, url: str, token: str) -> tuple[str, dict[str, Any]]:
    normalized = normalize_core_url(url)
    if not token.strip():
        raise CoreAuthError("A bearer token is required")
    client = CoreClient(async_get_clientsession(hass), normalized, token.strip(), timeout=10)
    info = await client.async_get_info()
    client.validate_info(info)
    return normalized, info


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
                timer_outputs = await _validate_timer_outputs(
                    self.hass, user_input.get(CONF_TIMER_OUTPUTS, {}),
                )
            except ValueError:
                errors["base"] = "invalid_timer_outputs"
                return self.async_show_form(
                    step_id="user", data_schema=self._user_schema(), errors=errors,
                )

            try:
                core_url, info = await _validate_core(
                    self.hass, user_input[CONF_CORE_URL], user_input[CONF_TOKEN],
                )
            except CoreAuthError:
                errors["base"] = "invalid_auth"
            except CoreProtocolError:
                errors["base"] = "unsupported_protocol"
            except (CoreError, ValueError, KeyError):
                errors["base"] = "cannot_connect"
            else:
                self._pending_config = {
                    CONF_CORE_URL: core_url,
                    CONF_TOKEN: user_input[CONF_TOKEN].strip(),
                    CONF_TIMER_OUTPUTS: timer_outputs,
                    "profile_choices": _profile_choices(info),
                }
                return await self.async_step_persona()

        return self.async_show_form(step_id="user", data_schema=self._user_schema(), errors=errors)

    async def async_step_persona(self, user_input: dict[str, Any] | None = None):
        pending = self._pending_config
        errors = {}
        if user_input is not None:
            profile_id = user_input.get(CONF_PROFILE_ID, "")
            if not isinstance(profile_id, str) or profile_id not in {
                option["value"] for option in pending["profile_choices"]
            }:
                errors["base"] = "invalid_persona"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_CORE_URL: pending[CONF_CORE_URL],
                    CONF_TOKEN: pending[CONF_TOKEN],
                    CONF_TIMER_OUTPUTS: pending[CONF_TIMER_OUTPUTS],
                }
                if profile_id:
                    data[CONF_PROFILE_ID] = profile_id
                return self.async_create_entry(title="SmartHome Assistant", data=data)

        return self.async_show_form(
            step_id="persona", data_schema=self._persona_schema(pending["profile_choices"]),
            errors=errors,
        )

    @staticmethod
    def _user_schema():
        return probatio.Schema({
            probatio.Required(CONF_CORE_URL): selector.TextSelector(),
            probatio.Required(CONF_TOKEN): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
            ),
            probatio.Optional(CONF_TIMER_OUTPUTS, default=dict): selector.ObjectSelector(),
        })

    @staticmethod
    def _persona_schema(choices, current_profile_id=""):
        return probatio.Schema({
            probatio.Required(CONF_PROFILE_ID, default=current_profile_id): _persona_selector(choices),
        })

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            try:
                timer_outputs = await _validate_timer_outputs(
                    self.hass,
                    user_input.get(
                        CONF_TIMER_OUTPUTS,
                        entry.options.get(CONF_TIMER_OUTPUTS, entry.data.get(CONF_TIMER_OUTPUTS, {})),
                    ),
                )
            except ValueError:
                errors["base"] = "invalid_timer_outputs"
                return self.async_show_form(
                    step_id="reconfigure", data_schema=self._reconfigure_schema(entry), errors=errors,
                )

            try:
                core_url, info = await _validate_core(
                    self.hass, user_input[CONF_CORE_URL], user_input[CONF_TOKEN],
                )
            except CoreAuthError:
                errors["base"] = "invalid_auth"
            except CoreProtocolError:
                errors["base"] = "unsupported_protocol"
            except (CoreError, ValueError, KeyError):
                errors["base"] = "cannot_connect"
            else:
                self._pending_config = {
                    CONF_CORE_URL: core_url,
                    CONF_TOKEN: user_input[CONF_TOKEN].strip(),
                    CONF_TIMER_OUTPUTS: timer_outputs,
                    "profile_choices": _profile_choices(info, _current_profile_id(entry)),
                }
                return await self.async_step_reconfigure_persona()

        return self.async_show_form(
            step_id="reconfigure", data_schema=self._reconfigure_schema(entry), errors=errors,
        )

    async def async_step_reconfigure_persona(self, user_input: dict[str, Any] | None = None):
        pending = self._pending_config
        entry = self._get_reconfigure_entry()
        profile_choices = pending["profile_choices"]
        errors = {}
        if user_input is not None:
            profile_id = user_input.get(CONF_PROFILE_ID, "")
            if not isinstance(profile_id, str) or profile_id not in {
                option["value"] for option in profile_choices
            }:
                errors["base"] = "invalid_persona"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_CORE_URL: pending[CONF_CORE_URL],
                        CONF_TOKEN: pending[CONF_TOKEN],
                        CONF_PROFILE_ID: profile_id,
                        CONF_TIMER_OUTPUTS: pending[CONF_TIMER_OUTPUTS],
                    },
                    options={
                        CONF_PROFILE_ID: profile_id,
                        CONF_TIMER_OUTPUTS: pending[CONF_TIMER_OUTPUTS],
                    },
                    reason="reconfigure_successful",
                )

        return self.async_show_form(
            step_id="reconfigure_persona",
            data_schema=self._persona_schema(profile_choices, _current_profile_id(entry)),
            errors=errors,
        )

    @staticmethod
    def _reconfigure_schema(entry):
        return probatio.Schema({
            probatio.Required(CONF_CORE_URL, default=entry.data[CONF_CORE_URL]): selector.TextSelector(),
            probatio.Required(CONF_TOKEN, default=entry.data[CONF_TOKEN]): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
            ),
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
        current_profile = _current_profile_id(self.config_entry)
        if not hasattr(self, "_profile_choices"):
            self._profile_choices = await self._async_load_profile_choices(current_profile)

        if user_input is not None:
            try:
                timer_outputs = await _validate_timer_outputs(
                    self.hass,
                    user_input.get(
                        CONF_TIMER_OUTPUTS,
                        self.config_entry.options.get(
                            CONF_TIMER_OUTPUTS, self.config_entry.data.get(CONF_TIMER_OUTPUTS, {}),
                        ),
                    ),
                )
            except ValueError:
                errors["base"] = "invalid_timer_outputs"
                return self.async_show_form(step_id="init", data_schema=self._options_schema(), errors=errors)

            profile_id = user_input.get(CONF_PROFILE_ID, current_profile)
            if not isinstance(profile_id, str) or profile_id not in {
                option["value"] for option in self._profile_choices
            }:
                errors["base"] = "invalid_persona"
            else:
                return self.async_create_entry(
                    title="",
                    data={CONF_PROFILE_ID: profile_id, CONF_TIMER_OUTPUTS: timer_outputs},
                )

        return self.async_show_form(step_id="init", data_schema=self._options_schema(), errors=errors)

    async def _async_load_profile_choices(self, retained_profile_id):
        entry = self.config_entry
        try:
            _, info = await _validate_core(
                self.hass, entry.data[CONF_CORE_URL], entry.data[CONF_TOKEN],
            )
        except (CoreError, ValueError):
            info = {}
        return _profile_choices(info, retained_profile_id)

    def _options_schema(self):
        current_timer_outputs = self.config_entry.options.get(
            CONF_TIMER_OUTPUTS, self.config_entry.data.get(CONF_TIMER_OUTPUTS, {}),
        )
        current_profile = _current_profile_id(self.config_entry)
        choices = getattr(self, "_profile_choices", _profile_choices({}, current_profile))
        return probatio.Schema({
            probatio.Optional(CONF_PROFILE_ID, default=current_profile): _persona_selector(choices),
            probatio.Optional(CONF_TIMER_OUTPUTS, default=current_timer_outputs): selector.ObjectSelector(),
        })
