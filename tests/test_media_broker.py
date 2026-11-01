"""MediaBroker against a real Home Assistant core with simulated Music Assistant.

Requires ``pytest-homeassistant-custom-component``; skipped otherwise. Music
Assistant actions are replaced by fakes that follow HA's music_assistant
service contract (search response schema, play_media, transfer_queue), and
media_player actions update simulated player state.
"""
from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import SupportsResponse  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import area_registry as ar  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.smarthome_assistant.media import MediaBroker, MediaError  # noqa: E402
from custom_components.smarthome_assistant.media_targets import TargetError  # noqa: E402
from custom_components.smarthome_assistant.media_websocket import async_setup_media  # noqa: E402

ALL_FEATURES = 4 | 8 | 524288 | 512 | 1 | 16384 | 4096 | 32 | 16  # volume, mute, grouping, playback


class FakeMusic:
    """Simulated Music Assistant + media_player actions with call recording."""

    def __init__(self, hass):
        self.hass = hass
        self.calls: list[tuple[str, str, dict]] = []
        self.results: dict = {"tracks": [], "artists": [], "albums": [], "playlists": [], "radio": [],
                              "audiobooks": [], "podcasts": []}
        self.library_results: dict | None = None
        self.reject_radio_mode = False

    def register(self):
        services = self.hass.services

        async def search(call):
            self.calls.append(("music_assistant", "search", dict(call.data)))
            if call.data.get("library_only") and self.library_results is not None:
                return self.library_results
            return self.results

        services.async_register("music_assistant", "search", search, supports_response=SupportsResponse.ONLY)
        for domain, service, handler in (
            ("music_assistant", "play_media", self._play),
            ("music_assistant", "transfer_queue", self._transfer),
            ("media_player", "join", self._join),
            ("media_player", "unjoin", self._unjoin),
            ("media_player", "volume_set", self._volume),
            ("media_player", "volume_mute", self._mute),
            ("media_player", "media_pause", self._state("paused")),
            ("media_player", "media_play", self._state("playing")),
            ("media_player", "media_stop", self._state("idle")),
            ("media_player", "media_next_track", self._state(None)),
            ("media_player", "media_previous_track", self._state(None)),
        ):
            services.async_register(domain, service, self._record(domain, service, handler))

    def _record(self, domain, service, handler):
        async def run(call):
            self.calls.append((domain, service, dict(call.data)))
            await handler(call)
        return run

    def update(self, entity_id, state=None, **attributes):
        current = self.hass.states.get(entity_id)
        merged = {**current.attributes, **attributes}
        self.hass.states.async_set(entity_id, state or current.state, merged)

    async def _play(self, call):
        if call.data.get("radio_mode") and self.reject_radio_mode:
            raise HomeAssistantError("Radio mode is not supported for this item")
        self.update(call.data["entity_id"], "playing")

    async def _transfer(self, call):
        self.update(call.data["source_player"], "idle")
        self.update(call.data["entity_id"], "playing")

    async def _join(self, call):
        leader = call.data["entity_id"]
        members = [leader, *call.data["group_members"]]
        for entity_id in members:
            self.update(entity_id, group_members=members)

    async def _unjoin(self, call):
        self.update(call.data["entity_id"], "idle", group_members=[])

    async def _volume(self, call):
        self.update(call.data["entity_id"], volume_level=call.data["volume_level"])

    async def _mute(self, call):
        self.update(call.data["entity_id"], is_volume_muted=call.data["is_volume_muted"])

    def _state(self, state):
        async def handler(call):
            if state:
                self.update(call.data["entity_id"], state)
        return handler

    def called(self, domain, service):
        return [data for call_domain, call_service, data in self.calls
                if (call_domain, call_service) == (domain, service)]


@pytest.fixture
async def home(hass):
    """Kitchen and lounge Dots, a saved all-Dots group, a TV and two satellites."""
    assert await async_setup_component(hass, "homeassistant", {})
    music_entry = MockConfigEntry(domain="music_assistant", state=ConfigEntryState.LOADED)
    music_entry.add_to_hass(hass)
    esphome_entry = MockConfigEntry(domain="esphome")
    esphome_entry.add_to_hass(hass)
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    lounge = areas.async_create("Lounge")
    devices = dr.async_get(hass)
    entities = er.async_get(hass)

    def satellite(name, area):
        device = devices.async_get_or_create(config_entry_id=esphome_entry.entry_id,
                                             identifiers={("esphome", name)}, name=f"{name} Dot device")
        devices.async_update_device(device.id, area_id=area.id)
        entry = entities.async_get_or_create("assist_satellite", "esphome", f"{name}_sat",
                                             suggested_object_id=f"{name}_dot", device_id=device.id)
        hass.states.async_set(entry.entity_id, "idle")
        return entry.entity_id

    def ma_player(object_id, name, area=None, *, group=False, members=(), volume=0.3):
        device = devices.async_get_or_create(config_entry_id=music_entry.entry_id,
                                             identifiers={("music_assistant", object_id)}, name=name)
        if area:
            devices.async_update_device(device.id, area_id=area.id)
        entry = entities.async_get_or_create("media_player", "music_assistant", object_id,
                                             suggested_object_id=object_id, device_id=device.id,
                                             config_entry=music_entry)
        hass.states.async_set(entry.entity_id, "idle", {
            "friendly_name": name, "supported_features": ALL_FEATURES, "volume_level": volume,
            "is_volume_muted": False, "group_members": list(members),
            "mass_player_type": "group" if group else "player"})
        async_expose_entity(hass, "conversation", entry.entity_id, True)
        return entry.entity_id

    ids = {
        "kitchen_sat": satellite("kitchen", kitchen),
        "lounge_sat": satellite("lounge", lounge),
        "kitchen": ma_player("kitchen_dot_ma", "Kitchen Dot", kitchen),
        "lounge": ma_player("lounge_dot_ma", "Lounge Dot", lounge, volume=0.5),
    }
    ids["all"] = ma_player("all_dots", "All Dots", group=True, members=[ids["kitchen"], ids["lounge"]])
    entities.async_update_entity(ids["all"], aliases={"everywhere", "whole house"})
    tv = entities.async_get_or_create("media_player", "cast", "tv", suggested_object_id="lounge_tv")
    entities.async_update_entity(tv.entity_id, area_id=lounge.id)
    hass.states.async_set(tv.entity_id, "on", {"friendly_name": "Lounge TV", "supported_features": 4,
                                               "volume_level": 0.2})
    async_expose_entity(hass, "conversation", tv.entity_id, True)
    ids["tv"] = tv.entity_id
    hidden = ma_player("garage_dot_ma", "Garage Dot")
    async_expose_entity(hass, "conversation", hidden, False)
    ids["hidden"] = hidden
    display = ma_player("frame_ma", "Photo Frame")
    fake = FakeMusic(hass)
    fake.update(display, mass_player_type="display")
    fake.register()
    broker = MediaBroker(hass)
    yield {"hass": hass, "broker": broker, "fake": fake, "ids": ids, "music_entry": music_entry}
    # The real Music Assistant component must not try to unload the mock entry.
    if hass.config_entries.async_get_entry(music_entry.entry_id):
        music_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


def song(name, artist, uri):
    return {"media_type": "track", "uri": uri, "name": name, "version": "", "image": None,
            "favorite": False, "artists": [{"media_type": "artist", "uri": f"spotify://artist/{artist}",
                                            "name": artist, "version": "", "image": None}]}


async def test_catalog_uses_exposure_and_audio_players_only(home):
    players = {player["entity_id"] for player in home["broker"].catalog()}
    ids = home["ids"]
    assert {ids["kitchen"], ids["lounge"], ids["all"], ids["tv"]} <= players
    assert ids["hidden"] not in players
    assert "media_player.frame_ma" not in players


async def test_info_resolves_satellite_to_speaker_in_its_area(home):
    info = await home["broker"].info({"source_satellite": home["ids"]["kitchen_sat"]})
    assert info["available"] and info["version"] == 1
    assert info["source"]["player"]["entity_id"] == home["ids"]["kitchen"]
    assert info["source"]["reason"] == "area"
    assert all("config_entry_id" not in player for player in info["players"])


async def test_play_song_on_source_with_mix(home):
    fake, ids = home["fake"], home["ids"]
    fake.results["tracks"] = [song("Hello", "Lionel Richie", "spotify://track/lr"),
                              song("Hello", "Adele", "spotify://track/adele")]
    result = await home["broker"].play({"kind": "track", "query": "hello", "artist": "adele",
                                        "source_satellite": ids["kitchen_sat"]})
    assert result["status"] == "playing"
    assert result["item"]["artists"] == ["Adele"] and result["mix"] is True
    search = fake.called("music_assistant", "search")[0]
    assert search["config_entry_id"] == home["music_entry"].entry_id
    assert search["artist"] == "adele" and search["media_type"] == ["track"]
    play = fake.called("music_assistant", "play_media")[0]
    assert play == {"entity_id": ids["kitchen"], "media_id": "spotify://track/adele",
                    "media_type": "track", "enqueue": "replace", "radio_mode": True}


async def test_play_on_dot_in_active_group_plays_on_group(home):
    fake, ids = home["fake"], home["ids"]
    fake.update(ids["all"], "playing")
    fake.results["artists"] = [{"media_type": "artist", "uri": "spotify://artist/ts", "name": "Taylor Swift",
                                "version": "", "image": None}]
    result = await home["broker"].play({"kind": "artist", "query": "taylor swift",
                                        "source_satellite": ids["kitchen_sat"]})
    assert result["target"]["entity_id"] == ids["all"]
    assert fake.called("music_assistant", "play_media")[0]["entity_id"] == ids["all"]


async def test_radio_mode_refusal_falls_back_once(home):
    fake = home["fake"]
    fake.reject_radio_mode = True
    fake.results["tracks"] = [song("Hello", "Adele", "spotify://track/adele")]
    result = await home["broker"].play({"kind": "track", "query": "hello",
                                        "source_satellite": home["ids"]["kitchen_sat"]})
    assert result["status"] == "playing"
    assert result["mix"] is False and result["mix_unavailable"] is True
    assert [call["radio_mode"] for call in fake.called("music_assistant", "play_media")] == [True, False]


async def test_unsplit_title_by_artist_picks_that_artist(home):
    fake = home["fake"]
    fake.results["tracks"] = [song("Hello", "Lionel Richie", "spotify://track/lr"),
                              song("Hello", "Adele", "spotify://track/adele")]
    result = await home["broker"].play({"kind": "track", "query": "hello by adele",
                                        "source_satellite": home["ids"]["kitchen_sat"]})
    assert result["item"]["uri"] == "spotify://track/adele"


async def test_unsplit_title_by_artist_searches_split_when_needed(home):
    fake = home["fake"]
    calls = []

    async def search(call):
        calls.append(dict(call.data))
        found = [song("Hello", "Adele", "spotify://track/adele")] if call.data.get("artist") else []
        return {**fake.results, "tracks": found}

    home["hass"].services.async_register("music_assistant", "search", search,
                                         supports_response=SupportsResponse.ONLY)
    result = await home["broker"].play({"kind": "track", "query": "hello by adele",
                                        "source_satellite": home["ids"]["kitchen_sat"]})
    assert result["item"]["uri"] == "spotify://track/adele"
    assert [(call["name"], call.get("artist")) for call in calls] == [
        ("hello by adele", None), ("hello", "adele")]


async def test_title_containing_by_still_plays(home):
    fake = home["fake"]
    fake.results["tracks"] = [song("Stand By Me", "Ben E. King", "spotify://track/sbm")]
    result = await home["broker"].play({"kind": "track", "query": "stand by me",
                                        "source_satellite": home["ids"]["kitchen_sat"]})
    assert result["item"]["uri"] == "spotify://track/sbm"


async def test_naming_a_dynamic_leader_changes_only_that_speaker(home):
    fake, ids = home["fake"], home["ids"]
    members = [ids["kitchen"], ids["lounge"]]
    fake.update(ids["kitchen"], "playing", group_members=members)
    fake.update(ids["lounge"], "playing", group_members=members)
    result = await home["broker"].volume({"mode": "step", "value": -10, "targets": ["kitchen"]})
    assert [item["entity_id"] for item in result["players"]] == [ids["kitchen"]]


async def test_not_found_never_plays(home):
    fake = home["fake"]
    fake.results["tracks"] = [song("Totally Different", "Band", "spotify://track/x")]
    result = await home["broker"].play({"kind": "track", "query": "bohemian rhapsody",
                                        "source_satellite": home["ids"]["kitchen_sat"]})
    assert result["status"] == "not_found"
    assert not fake.called("music_assistant", "play_media")


async def test_radio_station_prefers_library(home):
    fake = home["fake"]
    station = {"media_type": "radio", "uri": "library://radio/3", "name": "BBC Radio 1", "version": "",
               "image": None, "favorite": True}
    fake.library_results = {**fake.results, "radio": [station]}
    result = await home["broker"].play({"kind": "radio", "query": "radio one",
                                        "source_satellite": home["ids"]["lounge_sat"]})
    assert result["item"]["uri"] == "library://radio/3" and result["mix"] is False
    assert fake.called("music_assistant", "search")[0]["library_only"] is True
    assert fake.called("music_assistant", "play_media")[0]["entity_id"] == home["ids"]["lounge"]


async def test_play_everywhere_uses_saved_group(home):
    fake = home["fake"]
    fake.results["tracks"] = [song("Hello", "Adele", "spotify://track/adele")]
    result = await home["broker"].play({"kind": "track", "query": "hello", "scope": "all",
                                        "source_satellite": home["ids"]["kitchen_sat"]})
    assert result["target"]["entity_id"] == home["ids"]["all"]


async def test_play_without_a_speaker_asks(home):
    with pytest.raises(TargetError) as error:
        await home["broker"].play({"kind": "track", "query": "hello"})
    assert error.value.code == "no_target"


async def test_play_on_non_music_player_is_unsupported(home):
    with pytest.raises(TargetError) as error:
        await home["broker"].play({"kind": "track", "query": "hello", "targets": ["lounge tv"]})
    assert error.value.code == "unsupported"


async def test_unavailable_without_loaded_music_assistant(home):
    hass = home["hass"]
    await hass.config_entries.async_remove(home["music_entry"].entry_id)
    with pytest.raises(MediaError) as error:
        await home["broker"].play({"kind": "track", "query": "hello",
                                   "source_satellite": home["ids"]["kitchen_sat"]})
    assert error.value.code == "unavailable"


async def test_volume_step_changes_only_source_speaker(home):
    result = await home["broker"].volume({"mode": "step", "value": 10,
                                          "source_satellite": home["ids"]["kitchen_sat"]})
    assert result["status"] == "completed"
    assert [(item["entity_id"], item["previous"], item["volume"]) for item in result["players"]] == [
        (home["ids"]["kitchen"], 30, 40)]


async def test_volume_everywhere_steps_each_group_member_from_its_own_level(home):
    fake, ids = home["fake"], home["ids"]
    fake.update(ids["all"], "playing")
    result = await home["broker"].volume({"mode": "step", "value": -10, "scope": "all",
                                          "source_satellite": ids["kitchen_sat"]})
    levels = {item["entity_id"]: item["volume"] for item in result["players"]}
    assert levels == {ids["kitchen"]: 20, ids["lounge"]: 40}


async def test_volume_ceiling_and_unmute(home):
    broker, fake, ids = home["broker"], home["fake"], home["ids"]
    broker.configure({}, 60)
    fake.update(ids["kitchen"], is_volume_muted=True)
    result = await broker.volume({"mode": "set", "value": 90, "source_satellite": ids["kitchen_sat"]})
    player = result["players"][0]
    assert player["volume"] == 60 and player["limited"] is True
    assert fake.called("media_player", "volume_mute")[-1]["is_volume_muted"] is False


async def test_volume_on_named_tv(home):
    result = await home["broker"].volume({"mode": "set", "value": 15, "targets": ["lounge tv"]})
    assert result["players"][0]["entity_id"] == home["ids"]["tv"]
    assert home["fake"].called("media_player", "volume_set")[0]["volume_level"] == 0.15


async def test_add_speaker_joins_current_session(home):
    fake, ids = home["fake"], home["ids"]
    fake.update(ids["kitchen"], "playing")
    result = await home["broker"].speakers({"operation": "add", "targets": ["lounge"],
                                            "source_satellite": ids["kitchen_sat"]})
    assert result["status"] == "completed"
    assert fake.called("media_player", "join")[0] == {"entity_id": ids["kitchen"],
                                                      "group_members": [ids["lounge"]]}


async def test_add_everywhere_moves_to_saved_group(home):
    fake, ids = home["fake"], home["ids"]
    fake.update(ids["kitchen"], "playing")
    result = await home["broker"].speakers({"operation": "add", "scope": "all",
                                            "source_satellite": ids["kitchen_sat"]})
    assert result["session"]["entity_id"] == ids["all"]
    assert fake.called("music_assistant", "transfer_queue")[0] == {
        "entity_id": ids["all"], "source_player": ids["kitchen"], "auto_play": True}


async def test_remove_dynamic_leader_hands_queue_over(home):
    fake, ids = home["fake"], home["ids"]
    members = [ids["kitchen"], ids["lounge"]]
    fake.update(ids["kitchen"], "playing", group_members=members)
    fake.update(ids["lounge"], "playing", group_members=members)
    result = await home["broker"].speakers({"operation": "remove", "targets": ["kitchen"],
                                            "source_satellite": ids["kitchen_sat"]})
    assert result["status"] == "completed"
    assert fake.called("music_assistant", "transfer_queue")[0]["entity_id"] == ids["lounge"]
    assert not fake.called("media_player", "media_stop")


async def test_remove_last_speaker_stops(home):
    fake, ids = home["fake"], home["ids"]
    fake.update(ids["kitchen"], "playing")
    result = await home["broker"].speakers({"operation": "remove", "targets": ["kitchen"],
                                            "source_satellite": ids["kitchen_sat"]})
    assert result["status"] == "completed"
    assert fake.called("media_player", "media_stop")[0]["entity_id"] == ids["kitchen"]


async def test_remove_speaker_not_in_session_is_no_match(home):
    fake, ids = home["fake"], home["ids"]
    fake.update(ids["kitchen"], "playing")
    result = await home["broker"].speakers({"operation": "remove", "targets": ["lounge"],
                                            "source_satellite": ids["kitchen_sat"]})
    assert result["status"] == "no_match"


async def test_only_here_from_group_moves_queue(home):
    fake, ids = home["fake"], home["ids"]
    fake.update(ids["all"], "playing")
    await home["broker"].speakers({"operation": "only", "targets": ["kitchen"],
                                   "source_satellite": ids["kitchen_sat"]})
    # The kitchen is a member of the playing group: the group's other members leave.
    assert fake.called("media_player", "unjoin")[0]["entity_id"] == ids["lounge"]


async def test_pause_from_satellite_without_local_music_uses_only_session(home):
    fake, ids = home["fake"], home["ids"]
    fake.update(ids["lounge"], "playing")
    result = await home["broker"].control({"operation": "pause", "source_satellite": ids["kitchen_sat"]})
    assert result["players"][0]["entity_id"] == ids["lounge"]
    assert home["hass"].states.get(ids["lounge"]).state == "paused"


async def test_pause_with_two_sessions_asks(home):
    fake, ids = home["fake"], home["ids"]
    fake.update(ids["lounge"], "playing")
    fake.update(ids["kitchen"], "playing")
    with pytest.raises(TargetError) as error:
        await home["broker"].control({"operation": "pause"})
    assert error.value.code == "ambiguous_target"


async def test_websocket_commands_and_request_replay(home, hass_ws_client):
    hass, ids = home["hass"], home["ids"]
    broker = async_setup_media(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smarthome_assistant/media/info",
                            "source_satellite": ids["kitchen_sat"]})
    reply = await client.receive_json()
    assert reply["success"] and reply["result"]["source"]["player"]["entity_id"] == ids["kitchen"]
    request = {"type": "smarthome_assistant/media/volume", "request_id": "r-1", "mode": "step",
               "value": 10, "source_satellite": ids["kitchen_sat"]}
    await client.send_json({"id": 2, **request})
    first = await client.receive_json()
    await client.send_json({"id": 3, **request})
    second = await client.receive_json()
    assert first["result"] == second["result"]
    assert len(home["fake"].called("media_player", "volume_set")) == 1
    await client.send_json({"id": 4, "type": "smarthome_assistant/media/play", "request_id": "r-2",
                            "kind": "track", "query": "hello"})
    reply = await client.receive_json()
    assert reply["success"] and reply["result"]["status"] == "no_target"
    await client.send_json({"id": 5, "type": "smarthome_assistant/media/volume", "request_id": "r-3",
                            "mode": "set", "source_satellite": ids["kitchen_sat"]})
    reply = await client.receive_json()
    assert not reply["success"] and reply["error"]["code"] == "invalid_format"
    assert broker is hass.data["smarthome_assistant_media"]
