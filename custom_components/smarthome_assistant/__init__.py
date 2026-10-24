"""Home Assistant conversation-agent adapter for SmartHome Assistant Core."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

import logging

from .api import CoreClient, CoreError
from .const import (
    CONF_AUTH_METHOD, CONF_CORE_URL, CONF_SHOW_IN_SIDEBAR, CONF_SPEECH_ENABLED,
    CONF_TOKEN, CONF_TIMER_OUTPUTS, CONVERSATION_PLATFORM, DATA_SPEECH_CLIENT,
    DOMAIN, PLATFORMS, SENSOR_PLATFORM, SPEECH_PLATFORMS,
)
from .jobs_setup import async_setup_jobs, async_configure_outputs, async_configure_panel
from .speech import SpeechClient, speech_base_url_for_entry


async def async_setup(hass: HomeAssistant, config) -> bool:
    """Start durable home work independently of the remote conversation client."""
    await async_setup_jobs(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await async_setup_jobs(hass)
    await async_configure_outputs(hass, entry.options.get(
        CONF_TIMER_OUTPUTS, entry.data.get(CONF_TIMER_OUTPUTS, {})))
    await async_configure_panel(hass, entry.options.get(CONF_SHOW_IN_SIDEBAR, False))
    session = async_get_clientsession(hass)
    client = CoreClient(session, entry.data[CONF_CORE_URL], entry.data[CONF_TOKEN])
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = client
    speech_enabled = entry.options.get(
        CONF_SPEECH_ENABLED, entry.data.get(CONF_SPEECH_ENABLED, True),
    )
    if speech_enabled:
        domain_data.setdefault(DATA_SPEECH_CLIENT, {})[entry.entry_id] = SpeechClient(
            session, speech_base_url_for_entry(entry), entry.data[CONF_TOKEN],
        )
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(CONVERSATION_PLATFORM), Platform(SENSOR_PLATFORM)],
    )
    if speech_enabled:
        try:
            await hass.config_entries.async_forward_entry_setups(
                entry, [Platform(platform) for platform in SPEECH_PLATFORMS],
            )
        except Exception:
            # Speech backends are optional. Their outage must not take down the
            # independently useful conversation agent.
            logging.getLogger(__name__).warning(
                "SmartHome speech platforms could not be set up; conversation remains available",
                exc_info=True,
            )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, [Platform(p) for p in PLATFORMS])
    if unloaded:
        data = hass.data.get(DOMAIN, {})
        data.pop(entry.entry_id, None)
        data.get(DATA_SPEECH_CLIENT, {}).pop(entry.entry_id, None)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Revoke an integration-owned credential; manual credentials stay owner-managed."""
    await async_configure_panel(hass, False)
    if entry.data.get(CONF_AUTH_METHOD) != "paired":
        return
    client = CoreClient(async_get_clientsession(hass), entry.data[CONF_CORE_URL], entry.data[CONF_TOKEN])
    try:
        await client.async_revoke_pairing()
    except CoreError:
        logging.getLogger(__name__).warning(
            "Core pairing could not be revoked. Re-pair or clear Core's pairing state when it is available.")
