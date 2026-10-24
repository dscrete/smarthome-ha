"""Voice timers summary sensor.

SmartHome Core hides this entity from the model and, when it is a relevant
semantic match, offers its own timer contract instead (a promoting shadow rule
on ``platform`` + ``unique_id``). The entity therefore exists mainly so Capability
Index can embed timer wording; its state is only the active-timer count, with no
timer names or details in HA history.
"""
from __future__ import annotations

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .jobs import DOMAIN as JOBS_DOMAIN

VOICE_TIMERS_KEY = "voice_timers"
# Generic retrieval wording, set once; the owner may edit or remove them later.
VOICE_TIMERS_ALIASES = ["timers", "countdown timers"]
# HA's conversation assistant id for Assist exposure settings.
ASSIST = "conversation"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    unique_id = f"{entry.entry_id}_{VOICE_TIMERS_KEY}"
    first_creation = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id) is None
    async_add_entities([VoiceTimersSensor(hass.data[JOBS_DOMAIN], unique_id, first_creation)])


class VoiceTimersSensor(SensorEntity):
    """Number of active (pending, due or sounding) SmartHome voice timers."""

    _attr_has_entity_name = True
    _attr_name = "Voice timers"
    _attr_icon = "mdi:timer-outline"
    _attr_should_poll = False

    def __init__(self, manager, unique_id: str, first_creation: bool) -> None:
        self._manager = manager
        self._attr_unique_id = unique_id
        self._first_creation = first_creation

    @property
    def native_value(self) -> int:
        return self._manager.active_timer_count()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._manager.add_change_listener(self._changed))
        if self._first_creation:
            # Only when HA first registers the entity: later alias or exposure
            # changes belong to the owner and are never overwritten.
            self._first_creation = False
            registry = er.async_get(self.hass)
            entry = registry.async_get(self.entity_id)
            if entry is not None and not entry.aliases:
                registry.async_update_entity(self.entity_id, aliases=list(VOICE_TIMERS_ALIASES))
            async_expose_entity(self.hass, ASSIST, self.entity_id, True)

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()
