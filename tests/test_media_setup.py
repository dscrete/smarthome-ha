"""The whole integration loads with media support, options and the Music sensor."""
from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.homeassistant.exposed_entities import (  # noqa: E402
    async_expose_entity, async_should_expose,
)
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.smarthome_assistant.const import (  # noqa: E402
    CONF_MEDIA_MAX_VOLUME, CONF_MEDIA_PLAYER_MAP, DATA_MEDIA, DOMAIN, media_options,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def test_media_options_validation():
    assert media_options({}) == {CONF_MEDIA_PLAYER_MAP: {}, CONF_MEDIA_MAX_VOLUME: 100}
    assert media_options({CONF_MEDIA_PLAYER_MAP: {"assist_satellite.kitchen": "media_player.kitchen_ma"},
                          CONF_MEDIA_MAX_VOLUME: 70.0}) == {
        CONF_MEDIA_PLAYER_MAP: {"assist_satellite.kitchen": "media_player.kitchen_ma"},
        CONF_MEDIA_MAX_VOLUME: 70}
    for bad in ({CONF_MEDIA_PLAYER_MAP: {"media_player.x": "media_player.y"}},
                {CONF_MEDIA_PLAYER_MAP: {"assist_satellite.x": "light.y"}},
                {CONF_MEDIA_PLAYER_MAP: []},
                {CONF_MEDIA_MAX_VOLUME: 0}, {CONF_MEDIA_MAX_VOLUME: 101},
                {CONF_MEDIA_MAX_VOLUME: 50.5}, {CONF_MEDIA_MAX_VOLUME: True}):
        with pytest.raises(ValueError):
            media_options(bad)


async def test_integration_sets_up_media_and_music_sensor(hass, hass_ws_client):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={
        "core_url": "http://core.invalid:8765", "token": "test-token", "speech_enabled": False},
        options={"speech_enabled": False, CONF_MEDIA_MAX_VOLUME: 70,
                 CONF_MEDIA_PLAYER_MAP: {"assist_satellite.kitchen": "media_player.kitchen_ma"}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    broker = hass.data[DATA_MEDIA]
    assert broker.max_volume == 70
    assert broker.player_map == {"assist_satellite.kitchen": "media_player.kitchen_ma"}

    registry = er.async_get(hass)
    sensor_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_music")
    assert sensor_id is not None
    assert set(registry.async_get(sensor_id).aliases) == {"music", "songs", "radio station", "speakers", "volume"}
    assert async_should_expose(hass, "conversation", sensor_id)
    assert hass.states.get(sensor_id).state == "0"

    # A playing, exposed Music Assistant speaker is counted; a position-only
    # attribute update does not need to be.
    music_entry = MockConfigEntry(domain="music_assistant")
    music_entry.add_to_hass(hass)
    player = registry.async_get_or_create("media_player", "music_assistant", "dot",
                                          suggested_object_id="dot_ma", config_entry=music_entry)
    async_expose_entity(hass, "conversation", player.entity_id, True)
    hass.states.async_set(player.entity_id, "playing", {"mass_player_type": "player"})
    await hass.async_block_till_done()
    assert hass.states.get(sensor_id).state == "1"
    hass.states.async_set(player.entity_id, "idle", {"mass_player_type": "player"})
    await hass.async_block_till_done()
    assert hass.states.get(sensor_id).state == "0"

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smarthome_assistant/media/info"})
    reply = await client.receive_json()
    assert reply["success"]
    assert reply["result"]["available"] is False
    assert reply["result"]["max_volume"] == 70
    # Existing job commands still register alongside the media commands.
    await client.send_json({"id": 2, "type": "smarthome_jobs/list"})
    assert (await client.receive_json())["success"]
