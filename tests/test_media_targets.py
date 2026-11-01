"""Speaker resolution: no configuration, fail closed, Dot-in-a-group behaviour."""
import pytest


def player(entity_id, name, *, area="kitchen", music=True, group=False, members=(), state="idle",
           unique_id=None, device_id=None, device_name=None, aliases=()):
    return {"entity_id": entity_id, "name": name, "aliases": list(aliases), "area_id": area,
            "area_name": area.title() if area else None, "music_assistant": music, "is_group": group,
            "members": list(members), "state": state, "available": True,
            "unique_id": unique_id or entity_id.split(".")[1], "device_id": device_id,
            "device_name": device_name or name}


@pytest.fixture
def home():
    kitchen = player("media_player.kitchen_dot_2", "Kitchen Dot", unique_id="media_player.kitchen_dot")
    lounge = player("media_player.lounge_dot_2", "Lounge Dot", area="lounge")
    bedroom = player("media_player.bedroom_dot_2", "Bedroom Dot", area="bedroom")
    tv = player("media_player.lounge_tv", "Lounge TV", area="lounge", music=False, device_id="tvdev")
    everywhere = player("media_player.all_dots", "All Dots", area=None, group=True,
                        aliases=["everywhere", "whole house"],
                        members=[kitchen["entity_id"], lounge["entity_id"], bedroom["entity_id"]])
    return {"kitchen": kitchen, "lounge": lounge, "bedroom": bedroom, "tv": tv, "all": everywhere,
            "players": [kitchen, lounge, bedroom, tv, everywhere]}


def source(**values):
    base = {"satellite": "assist_satellite.dot", "device_id": "dotdev", "device_name": "Somewhere",
            "area_id": None, "mac": None, "device_entity_ids": []}
    return {**base, **values}


def test_source_links_ma_player_wrapping_the_dot_entity(media_targets, home):
    ctx = source(device_entity_ids=["media_player.kitchen_dot", "assist_satellite.dot"], area_id="lounge")
    result = media_targets.resolve_source(ctx, home["players"])
    assert result == {"player": home["kitchen"], "reason": "wrapped_entity"}


def test_source_links_by_mac_in_player_id(media_targets, home):
    home["bedroom"]["unique_id"] = "sendspin-AA:BB:CC:DD:EE:FF".lower()
    result = media_targets.resolve_source(source(mac="aabbccddeeff"), home["players"])
    assert result["player"] is home["bedroom"] and result["reason"] == "mac"


def test_source_links_by_exact_device_name(media_targets, home):
    result = media_targets.resolve_source(source(device_name="Lounge Dot"), home["players"])
    assert result["player"] is home["lounge"] and result["reason"] == "device_name"


def test_source_falls_back_to_only_speaker_in_area(media_targets, home):
    result = media_targets.resolve_source(source(area_id="bedroom"), home["players"])
    assert result["player"] is home["bedroom"] and result["reason"] == "area"


def test_source_ignores_non_music_players_in_area(media_targets, home):
    # The lounge has a TV and one Dot: the Dot is still the only MA speaker.
    result = media_targets.resolve_source(source(area_id="lounge"), home["players"])
    assert result["player"] is home["lounge"]


def test_source_fails_closed_when_area_is_ambiguous(media_targets, home):
    extra = player("media_player.kitchen_two", "Kitchen Two")
    result = media_targets.resolve_source(source(area_id="kitchen"), home["players"] + [extra])
    assert result == {"player": None, "reason": "none"}


def test_override_wins_and_missing_override_fails_closed(media_targets, home):
    assert media_targets.resolve_source(source(area_id="bedroom"), home["players"],
                                        "media_player.lounge_tv")["player"] is home["tv"]
    assert media_targets.resolve_source(source(area_id="bedroom"), home["players"],
                                        "media_player.gone") == {"player": None, "reason": "override_unavailable"}


def test_member_of_playing_saved_group_plays_as_group(media_targets, home):
    home["all"]["state"] = "playing"
    assert media_targets.session_for(home["kitchen"], home["players"]) is home["all"]


def test_idle_saved_group_does_not_capture_member(media_targets, home):
    assert media_targets.session_for(home["kitchen"], home["players"]) is home["kitchen"]


def test_dynamic_group_member_plays_through_leader(media_targets, home):
    members = [home["lounge"]["entity_id"], home["kitchen"]["entity_id"]]
    home["lounge"]["members"] = home["kitchen"]["members"] = members
    assert media_targets.session_for(home["kitchen"], home["players"]) is home["lounge"]
    assert media_targets.session_for(home["lounge"], home["players"]) is home["lounge"]


def test_everywhere_is_the_group_containing_the_source(media_targets, home):
    assert media_targets.everywhere_group(home["kitchen"], home["players"]) is home["all"]


def test_everywhere_prefers_largest_group(media_targets, home):
    upstairs = player("media_player.upstairs", "Upstairs", area=None, group=True,
                      members=[home["kitchen"]["entity_id"]])
    players = home["players"] + [upstairs]
    assert media_targets.everywhere_group(home["kitchen"], players) is home["all"]


def test_everywhere_without_group_returns_none(media_targets, home):
    players = [item for item in home["players"] if not item["is_group"]]
    assert media_targets.everywhere_group(home["kitchen"], players) is None


def test_names_aliases_and_areas(media_targets, home):
    players = home["players"]
    assert media_targets.resolve_name("everywhere", players) == [home["all"]]
    assert media_targets.resolve_name("Kitchen Dot", players) == [home["kitchen"]]
    assert media_targets.resolve_name("media_player.lounge_tv", players) == [home["tv"]]
    assert media_targets.resolve_name("the bedroom", players) == [home["bedroom"]]


def test_area_with_several_speakers_returns_all(media_targets, home):
    found = media_targets.resolve_name("lounge", home["players"])
    assert {item["entity_id"] for item in found} == {"media_player.lounge_dot_2", "media_player.lounge_tv"}


def test_raw_device_player_is_deduplicated_against_its_ma_wrapper(media_targets, home):
    raw = player("media_player.kitchen_dot", "Kitchen Dot", music=False)
    found = media_targets.resolve_name("kitchen dot", home["players"] + [raw])
    assert found == [home["kitchen"]]


def test_duplicate_names_are_ambiguous(media_targets, home):
    twin = player("media_player.other", "Kitchen Dot", area="garage", device_name="Garage Dot")
    with pytest.raises(media_targets.TargetError) as error:
        media_targets.resolve_name("kitchen dot", home["players"] + [twin])
    assert error.value.code == "ambiguous_target"
    assert len(error.value.choices) == 2


def test_unknown_name_is_not_found(media_targets, home):
    with pytest.raises(media_targets.TargetError) as error:
        media_targets.resolve_name("garage", home["players"])
    assert error.value.code == "not_found"


def test_source_scope_without_speaker_asks(media_targets, home):
    with pytest.raises(media_targets.TargetError) as error:
        media_targets.resolve_targets(None, None, source=source(), players=home["players"])
    assert error.value.code == "no_target"


def test_members_of_expands_groups(media_targets, home):
    assert media_targets.members_of(home["all"], home["players"]) == [
        home["kitchen"], home["lounge"], home["bedroom"]]
    assert media_targets.members_of(home["tv"], home["players"]) == [home["tv"]]
