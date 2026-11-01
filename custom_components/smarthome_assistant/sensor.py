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
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DATA_MEDIA, DOMAIN
from .jobs import DOMAIN as JOBS_DOMAIN

VOICE_TIMERS_KEY = "voice_timers"
# Generic retrieval wording, set once; the owner may edit or remove them later.
VOICE_TIMERS_ALIASES = ["timers", "countdown timers"]
MUSIC_KEY = "music"
MUSIC_ALIASES = ["music", "songs", "radio station", "speakers"]
# HA's conversation assistant id for Assist exposure settings.
ASSIST = "conversation"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    registry = er.async_get(hass)
    entities = []
    for kind in (VOICE_TIMERS_KEY, "alarms", "reminders", MUSIC_KEY):
        unique_id = f"{entry.entry_id}_{kind}"
        first_creation = registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None
        if kind == VOICE_TIMERS_KEY:
            entities.append(VoiceTimersSensor(hass.data[JOBS_DOMAIN], unique_id, first_creation))
        elif kind == MUSIC_KEY:
            entities.append(MusicSensor(hass.data[DATA_MEDIA], unique_id, first_creation))
        else:
            entities.append(ScheduledAlertsSensor(hass.data[JOBS_DOMAIN], unique_id, first_creation, kind[:-1]))
    async_add_entities(entities)


class VoiceTimersSensor(SensorEntity):
    """Number of active (pending, paused, due or sounding) voice timers."""

    _attr_has_entity_name = True
    _attr_name = "Voice timers"
    _attr_icon = "mdi:timer-outline"
    _attr_should_poll = False
    _aliases = VOICE_TIMERS_ALIASES

    def __init__(self, manager, unique_id: str, first_creation: bool) -> None:
        self._manager = manager
        self._attr_unique_id = unique_id
        self._first_creation = first_creation

    @property
    def native_value(self) -> int:
        return self._manager.active_timer_count()

    async def async_added_to_hass(self) -> None:
        self._subscribe()
        if self._first_creation:
            # Only when HA first registers the entity: later alias or exposure
            # changes belong to the owner and are never overwritten.
            self._first_creation = False
            registry = er.async_get(self.hass)
            entry = registry.async_get(self.entity_id)
            if entry is not None and not entry.aliases:
                registry.async_update_entity(self.entity_id, aliases=list(self._aliases))
            async_expose_entity(self.hass, ASSIST, self.entity_id, True)

    def _subscribe(self) -> None:
        self.async_on_remove(self._manager.add_change_listener(self._changed))

    @callback
    def _changed(self, *_args) -> None:
        self.async_write_ha_state()


class ScheduledAlertsSensor(VoiceTimersSensor):
    """Discovery/count only: never publish saved reminder contents to history."""

    def __init__(self, manager, unique_id, first_creation, kind):
        super().__init__(manager, unique_id, first_creation)
        self._kind = kind
        self._attr_name = "Alarms" if kind == "alarm" else "Reminders"
        self._attr_icon = "mdi:alarm" if kind == "alarm" else "mdi:reminder"
        self._aliases = (["alarms", "wake up alarms", "wake me"] if kind == "alarm"
                         else ["reminders", "remind me", "spoken reminders"])

    @property
    def native_value(self) -> int:
        return sum(record.get("kind") == self._kind and record.get("enabled", False)
                   for record in getattr(self._manager, "schedules", {}).values())


class MusicSensor(VoiceTimersSensor):
    """Count of exposed Music Assistant speakers playing; a retrieval anchor only.

    Core shadows it like the timer sensors and offers its music contract for
    play and speaker-grouping requests. It records no titles.
    """

    _attr_name = "Music"
    _attr_icon = "mdi:music"
    _aliases = MUSIC_ALIASES

    @property
    def native_value(self) -> int:
        return self._manager.playing_count()

    def _subscribe(self) -> None:
        @callback
        def media_players_only(event_data) -> bool:
            # Only playback-state transitions change the count; attribute
            # updates such as track position do not.
            if not str(event_data.get("entity_id", "")).startswith("media_player."):
                return False
            old, new = event_data.get("old_state"), event_data.get("new_state")
            return getattr(old, "state", None) != getattr(new, "state", None)

        self.async_on_remove(self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._changed, event_filter=media_players_only))
