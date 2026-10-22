"""Home Assistant conversation-agent adapter for SmartHome Assistant Core."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

import logging

from .api import CoreClient, CoreError
from .const import CONF_AUTH_METHOD, CONF_CORE_URL, CONF_TOKEN, CONF_TIMER_OUTPUTS, DOMAIN, PLATFORMS
from .jobs_setup import async_setup_jobs, async_configure_outputs


async def async_setup(hass: HomeAssistant, config) -> bool:
    """Start durable home work independently of the remote conversation client."""
    await async_setup_jobs(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await async_setup_jobs(hass)
    await async_configure_outputs(hass, entry.options.get(
        CONF_TIMER_OUTPUTS, entry.data.get(CONF_TIMER_OUTPUTS, {})))
    session = async_get_clientsession(hass)
    client = CoreClient(session, entry.data[CONF_CORE_URL], entry.data[CONF_TOKEN])
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(platform) for platform in PLATFORMS],
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, [Platform(platform) for platform in PLATFORMS],
    )
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Revoke an integration-owned credential; manual credentials stay owner-managed."""
    if entry.data.get(CONF_AUTH_METHOD) != "paired":
        return
    client = CoreClient(async_get_clientsession(hass), entry.data[CONF_CORE_URL], entry.data[CONF_TOKEN])
    try:
        await client.async_revoke_pairing()
    except CoreError:
        logging.getLogger(__name__).warning(
            "Core pairing could not be revoked. Re-pair or clear Core's pairing state when it is available.")
