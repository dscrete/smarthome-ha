"""Music Assistant playback, speaker grouping, volume and transport for Core.

Core interprets speech and sends only slots (kind, query, artist, targets,
volume words). This broker resolves speakers from HA's registries, searches
Music Assistant, picks a match deterministically, calls existing HA actions
and reports what actually happened. It owns no persistent state.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import time
from typing import Any, Awaitable, Callable

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import media_match
from .ha_native_timer_output import device_mac
from .media_targets import (
    ACTIVE_STATES, TargetError, brief, everywhere_group, expand_groups, members_of, resolve_name,
    resolve_source, resolve_targets, session_for,
)

_LOGGER = logging.getLogger(__name__)

MA_DOMAIN = "music_assistant"
ASSIST = "conversation"
PROTOCOL_VERSION = 1
# MA player types that render audio; display/light/visualizer/source do not.
AUDIO_PLAYER_TYPES = {"player", "stereo_pair", "group"}
ENQUEUE_OPTIONS = ("replace", "play", "next", "add")
SPEAKER_OPERATIONS = ("add", "remove", "move", "only")
VOLUME_MODES = ("set", "step", "mute", "unmute")
CONTROL_SERVICES = {"pause": "media_pause", "resume": "media_play", "stop": "media_stop",
                    "next": "media_next_track", "previous": "media_previous_track"}
MIX_KINDS = {"track", "artist"}
CALL_TIMEOUT = 20
PLAY_CONFIRM_SECONDS = 6
RECEIPT_LIMIT = 256
RECEIPT_TTL = 600


class MediaError(Exception):
    """An expected, speakable failure with a stable code."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


class MediaBroker:
    """Stateless media operations plus a short in-memory request receipt cache."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.player_map: dict[str, str] = {}
        self.max_volume = 100
        self._receipts: OrderedDict[tuple[str, str], tuple[float, asyncio.Future]] = OrderedDict()

    def configure(self, player_map: dict[str, str], max_volume: int) -> None:
        self.player_map = dict(player_map)
        self.max_volume = int(max_volume)

    # ----- discovery -------------------------------------------------------

    def available(self) -> bool:
        return any(entry.state is ConfigEntryState.LOADED
                   for entry in self.hass.config_entries.async_entries(MA_DOMAIN))

    def catalog(self) -> list[dict[str, Any]]:
        """Describe every Assist-exposed media player from HA's own registries."""
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        areas = ar.async_get(self.hass)
        players = []
        for entry in list(entities.entities.values()):
            if entry.domain != "media_player" or entry.disabled_by is not None:
                continue
            if not async_should_expose(self.hass, ASSIST, entry.entity_id):
                continue
            state = self.hass.states.get(entry.entity_id)
            attributes = state.attributes if state else {}
            is_music = entry.platform == MA_DOMAIN
            player_type = attributes.get("mass_player_type")
            if is_music and player_type is not None and str(player_type) not in AUDIO_PLAYER_TYPES:
                continue
            device = devices.async_get(entry.device_id) if entry.device_id else None
            area_id = entry.area_id or getattr(device, "area_id", None)
            area = areas.async_get_area(area_id) if area_id else None
            features = attributes.get("supported_features", 0)
            features = features if isinstance(features, int) and not isinstance(features, bool) else 0
            level = attributes.get("volume_level")
            members = [member for member in attributes.get("group_members") or ()
                       if isinstance(member, str)]
            players.append({
                "entity_id": entry.entity_id,
                "unique_id": entry.unique_id,
                "config_entry_id": entry.config_entry_id,
                "name": attributes.get("friendly_name") or entry.name or entry.original_name or entry.entity_id,
                "aliases": sorted(str(alias) for alias in (entry.aliases or ()) if isinstance(alias, str)),
                "device_id": entry.device_id,
                "device_name": (getattr(device, "name_by_user", None) or getattr(device, "name", None)),
                "area_id": area_id,
                "area_name": getattr(area, "name", None),
                "music_assistant": is_music,
                "is_group": bool(is_music and str(player_type) == "group"),
                "members": members,
                "state": state.state if state else "unavailable",
                "available": bool(state and state.state not in {"unavailable", "unknown"}),
                "volume": (round(level * 100) if isinstance(level, (int, float))
                           and not isinstance(level, bool) else None),
                "muted": attributes.get("is_volume_muted"),
                "can_set_volume": bool(features & MediaPlayerEntityFeature.VOLUME_SET),
                "can_mute": bool(features & MediaPlayerEntityFeature.VOLUME_MUTE),
                "can_group": bool(features & MediaPlayerEntityFeature.GROUPING),
                "now_playing": {key: attributes.get(f"media_{key}") for key in ("title", "artist", "album_name")},
            })
        return players

    def source_context(self, satellite: str | None, device_id: str | None) -> dict[str, Any]:
        """Describe the requesting satellite's physical identity, from HA only."""
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        entry = entities.async_get(satellite) if isinstance(satellite, str) else None
        if entry is not None and entry.domain != "assist_satellite":
            entry = None
        if entry is not None:
            device_id = entry.device_id
        device = devices.async_get(device_id) if isinstance(device_id, str) else None
        area_id = getattr(entry, "area_id", None) or getattr(device, "area_id", None)
        context = {
            "satellite": entry.entity_id if entry else None,
            "device_id": device.id if device else None,
            "device_name": (getattr(device, "name_by_user", None) or getattr(device, "name", None)),
            "area_id": area_id,
            "mac": device_mac(device),
            "device_entity_ids": ([item.entity_id for item in er.async_entries_for_device(entities, device.id)]
                                  if device else []),
        }
        return context

    def _source(self, request: dict[str, Any]) -> tuple[dict, str | None]:
        """The requesting satellite's identity and its configured override, if any."""
        context = self.source_context(request.get("source_satellite"), request.get("device_id"))
        return context, self.player_map.get(context["satellite"]) if context["satellite"] else None

    def _selection(self, request: dict[str, Any]) -> tuple[dict, list[dict], dict]:
        players = self.catalog()
        context, override = self._source(request)
        selection = resolve_targets(request.get("targets"), request.get("scope"), source=context,
                                    players=players, override=override)
        return context, players, selection

    # ----- read ------------------------------------------------------------

    async def info(self, request: dict[str, Any]) -> dict[str, Any]:
        players = self.catalog()
        context, override = self._source(request)
        origin = resolve_source(context, players, override)
        session = session_for(origin["player"], players)
        return {
            "version": PROTOCOL_VERSION,
            "available": self.available(),
            "max_volume": self.max_volume,
            "players": [public(player) for player in players],
            "source": {
                "player": brief(origin["player"]) if origin["player"] else None,
                "reason": origin["reason"],
                "session": brief(session) if session else None,
            },
        }

    # ----- play ------------------------------------------------------------

    async def play(self, request: dict[str, Any]) -> dict[str, Any]:
        kind = request["kind"]
        query = _text(request.get("query"), "query")
        artist = _text(request.get("artist"), "artist", required=False)
        enqueue = request.get("enqueue") or "replace"
        if kind not in media_match.KINDS or enqueue not in ENQUEUE_OPTIONS:
            raise ValueError("unsupported kind or enqueue option")
        if request.get("scope") == "playing":
            raise TargetError("invalid_target", "Choose a speaker, a room or everywhere to play music")
        if not self.available():
            raise MediaError("unavailable", "Music Assistant is not loaded")
        _context, players, selection = self._selection(request)
        origin = selection["source"]["player"]
        if selection["scope"] == "all":
            group = everywhere_group(origin, players)
            targets = [group] if group else [player for player in players if player.get("music_assistant")
                                             and not player.get("is_group") and player.get("available")]
            if not targets:
                raise TargetError("no_target", "No Music Assistant speakers are exposed to Assist")
        else:
            targets = selection["players"]
        not_music = [player for player in targets if not player.get("music_assistant")]
        if not_music:
            raise TargetError("unsupported", "Music can only play on Music Assistant speakers", not_music)
        if selection["scope"] == "all" or len(targets) > 1:
            leader = origin if origin in targets and not targets[0].get("is_group") else targets[0]
        else:
            leader = session_for(targets[0], players)
        if not leader.get("available"):
            raise MediaError("unavailable", f"{leader['name']} is unavailable", target=brief(leader))

        found = await self._find(kind, query, artist, leader["config_entry_id"])
        if found["item"] is None:
            return {"status": "not_found", "suggestion": found["suggestion"], "target": brief(leader)}
        item = found["item"]
        mix_wanted = request.get("radio_mode")
        mix = (kind in MIX_KINDS) if mix_wanted is None else bool(mix_wanted)
        mix_unavailable = False
        data = {"entity_id": leader["entity_id"], "media_id": item["uri"], "media_type": kind,
                "enqueue": enqueue}
        try:
            try:
                await self._call(MA_DOMAIN, "play_media", {**data, "radio_mode": mix})
            except MediaError as error:
                if not mix or error.code != "failed":
                    raise
                # Playing is idempotent with replace; retry once without the mix.
                _LOGGER.info("Radio mode unavailable for %s; playing without it", item["uri"])
                mix, mix_unavailable = False, True
                await self._call(MA_DOMAIN, "play_media", {**data, "radio_mode": False})
        except MediaError as error:
            if error.code == "unknown":
                return {"status": "unknown", "item": media_match.describe(item), "target": brief(leader)}
            raise

        joined, not_joined = await self._join(leader, [player for player in targets
                                                      if player["entity_id"] != leader["entity_id"]])
        started = await self._wait_state(leader["entity_id"], {"playing"}, PLAY_CONFIRM_SECONDS)
        return {
            "status": "playing" if started else "requested",
            "item": media_match.describe(item),
            "score": found["score"],
            "mix": mix,
            "mix_unavailable": mix_unavailable,
            "target": brief(leader),
            "joined": joined,
            "not_joined": not_joined,
        }

    async def _find(self, kind: str, query: str, artist: str | None, config_entry_id: str | None):
        if not config_entry_id:
            raise MediaError("unavailable", "The speaker has no Music Assistant connection")
        attempts = [(query, artist)]
        if kind in {"track", "album"} and not artist and (split := media_match.split_by_artist(query)):
            attempts.append(split)
        scopes = (True, False) if kind == "radio" else (False,)
        suggestion, best = None, 0.0
        for name, by in attempts:
            for library_only in scopes:
                data = {"config_entry_id": config_entry_id, "name": name, "media_type": [kind],
                        "limit": 10, "library_only": library_only}
                if by:
                    data["artist"] = by
                response = await self._call(MA_DOMAIN, "search", data, response=True) or {}
                items = response.get(media_match.RESULT_KEYS[kind]) or []
                choice = media_match.choose_spoken(kind, name, items, artist=by)
                if choice["item"] is not None:
                    return choice
                if choice["suggestion"] and choice["score"] > best:
                    suggestion, best = choice["suggestion"], choice["score"]
        return {"item": None, "score": best, "suggestion": suggestion}

    async def _join(self, leader: dict, others: list[dict]) -> tuple[list, list]:
        joined, not_joined = [], []
        already = set(leader.get("members") or ())
        wanted = [player for player in others if player["entity_id"] not in already]
        joined.extend(brief(player) for player in others if player["entity_id"] in already)
        if not wanted:
            return joined, not_joined
        if not leader.get("can_group"):
            return joined, [{**brief(player), "reason": "unsupported"} for player in wanted]
        groupable = [player for player in wanted if player.get("can_group") and player.get("available")]
        not_joined.extend({**brief(player), "reason": "unsupported" if player.get("available") else "unavailable"}
                          for player in wanted if player not in groupable)
        if groupable:
            try:
                await self._call("media_player", "join", {
                    "entity_id": leader["entity_id"],
                    "group_members": [player["entity_id"] for player in groupable]})
                joined.extend(brief(player) for player in groupable)
            except MediaError as error:
                not_joined.extend({**brief(player), "reason": error.code} for player in groupable)
        return joined, not_joined

    # ----- speakers --------------------------------------------------------

    def _active_session(self, request: dict, players: list[dict], origin: dict | None) -> dict:
        """The playback a speaker command changes: named, local, else the only one."""
        if request.get("session"):
            matches = resolve_name(request["session"], players)
            if len(matches) != 1:
                raise TargetError("ambiguous_target", "Choose one speaker that is playing", matches)
            return session_for(matches[0], players)
        local = session_for(origin, players) if origin else None
        if local and local.get("state") in ACTIVE_STATES:
            return local
        sessions = _active_sessions(players)
        if len(sessions) == 1:
            return sessions[0]
        if not sessions:
            raise MediaError("not_playing", "No music is playing")
        raise TargetError("ambiguous_target", "Music is playing on more than one speaker", sessions)

    async def speakers(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request["operation"]
        if operation not in SPEAKER_OPERATIONS:
            raise ValueError("unsupported speaker operation")
        if not request.get("targets") and request.get("scope") != "all":
            raise TargetError("no_target", "Say which speaker to change")
        if not self.available():
            raise MediaError("unavailable", "Music Assistant is not loaded")
        _context, players, selection = self._selection(request)
        origin = selection["source"]["player"]
        session = self._active_session(request, players, origin)
        members = {player["entity_id"] for player in members_of(session, players)}

        if selection["scope"] == "all":
            group = everywhere_group(origin or session, players)
            if group is None:
                targets = [player for player in players if player.get("music_assistant")
                           and not player.get("is_group")]
            elif operation in {"add", "move", "only"}:
                if group["entity_id"] == session["entity_id"]:
                    return {"status": "completed", "session": brief(group), "changed": []}
                await self._transfer(session, group)
                return {"status": "completed", "session": brief(group), "moved_from": brief(session),
                        "changed": [brief(group)]}
            else:
                targets = expand_groups(group, players)
        else:
            targets = selection["players"]
        targets = [player for player in targets if player.get("music_assistant")]
        if not targets:
            raise TargetError("unsupported", "Only Music Assistant speakers can be grouped")

        if operation == "move" or (operation == "only" and len(targets) == 1
                                    and targets[0]["entity_id"] != session["entity_id"]
                                    and targets[0]["entity_id"] not in members):
            if len(targets) != 1:
                raise TargetError("ambiguous_target", "Choose one speaker to move the music to", targets)
            await self._transfer(session, targets[0])
            return {"status": "completed", "session": brief(targets[0]), "moved_from": brief(session),
                    "changed": [brief(targets[0])]}

        # Membership changes act on physical speakers, so a named group means its members.
        speakers = _unique([member for player in targets for member in expand_groups(player, players)])
        if operation == "add":
            joined, not_joined = await self._join(session, [player for player in speakers
                                                            if player["entity_id"] not in members])
            already = [brief(player) for player in speakers if player["entity_id"] in members]
            return _receipt({"session": brief(session), "changed": joined, "failed": not_joined,
                             "already": already})
        changed, failed = [], []
        keep = {player["entity_id"] for player in speakers}
        current = members_of(session, players)
        if operation == "only":
            joined, failed = await self._join(session, [player for player in speakers
                                                        if player["entity_id"] not in members])
            changed.extend(joined)
            remaining = ([player for player in current if player["entity_id"] in keep]
                         + [player for player in speakers if player["entity_id"] in
                            {item["entity_id"] for item in joined}])
            remove = [player for player in current if player["entity_id"] not in keep]
            missing = []
        else:
            remove = [player for player in speakers if player["entity_id"] in members]
            remaining = [player for player in current if player["entity_id"] not in keep]
            missing = [brief(player) for player in speakers if player["entity_id"] not in members]
        removed, not_removed = await self._remove(session, remove, _unique(remaining))
        changed.extend(removed)
        failed.extend(not_removed)
        unchanged = operation == "only" and not remove and not changed and not failed
        return _receipt({"session": brief(session), "changed": changed, "failed": failed,
                         "already": [brief(player) for player in speakers] if unchanged else [],
                         "not_in_session": missing})

    async def _remove(self, session: dict, remove: list[dict], remaining: list[dict]):
        """Take speakers out of a session; removing all of them stops it."""
        changed, failed = [], []
        if not remove:
            return changed, failed
        if not remaining:
            await self._call("media_player", "media_stop", {"entity_id": session["entity_id"]})
            return [brief(player) for player in remove], failed
        leader_removed = not session.get("is_group") and any(
            player["entity_id"] == session["entity_id"] for player in remove)
        if leader_removed:
            # A dynamic group's leader owns the queue: hand it to a remaining
            # member first, then regroup the rest around that member.
            new_leader = remaining[0]
            await self._transfer(session, new_leader)
            _rejoined, not_rejoined = await self._join(new_leader, remaining[1:])
            failed.extend(not_rejoined)
            changed.append(brief(session))
            remove = [player for player in remove if player["entity_id"] != session["entity_id"]]
        for player in remove:
            try:
                await self._call("media_player", "unjoin", {"entity_id": player["entity_id"]})
                changed.append(brief(player))
            except MediaError as error:
                failed.append({**brief(player), "reason": error.code})
        return changed, failed

    async def _transfer(self, source: dict, target: dict) -> None:
        if not target.get("available"):
            raise MediaError("unavailable", f"{target['name']} is unavailable", target=brief(target))
        await self._call(MA_DOMAIN, "transfer_queue", {
            "entity_id": target["entity_id"], "source_player": source["entity_id"], "auto_play": True})

    # ----- volume ----------------------------------------------------------

    async def volume(self, request: dict[str, Any]) -> dict[str, Any]:
        mode = request["mode"]
        value = request.get("value")
        if mode not in VOLUME_MODES:
            raise ValueError("unsupported volume mode")
        if mode in {"set", "step"} and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("volume set and step need an integer value")
        if mode == "set" and not 0 <= value <= 100 or mode == "step" and not -100 <= value <= 100:
            raise ValueError("volume value is out of range")
        _context, players, selection = self._selection(request)
        origin = selection["source"]["player"]
        if selection["scope"] == "all":
            session = session_for(origin, players) if origin else None
            if session and len(members_of(session, players)) > 1:
                chosen = members_of(session, players)
            else:
                chosen = [player for player in players if player.get("state") == "playing"]
                if not chosen and origin:
                    chosen = [origin]
        else:
            chosen = selection["players"]
        speakers: list[dict] = []
        for player in chosen:
            for member in expand_groups(player, players):
                if member["entity_id"] not in {item["entity_id"] for item in speakers}:
                    speakers.append(member)
        if not speakers:
            raise TargetError("no_target", "No speaker is playing; say which speaker to change")
        results = await asyncio.gather(*(self._apply_volume(player, mode, value) for player in speakers))
        return _receipt({"players": list(results)})

    async def _apply_volume(self, player: dict, mode: str, value: int | None) -> dict:
        result = {**brief(player), "previous": player.get("volume"), "volume": player.get("volume")}
        try:
            if mode in {"mute", "unmute"}:
                if not player.get("can_mute"):
                    return {**result, "status": "unsupported"}
                await self._call("media_player", "volume_mute", {
                    "entity_id": player["entity_id"], "is_volume_muted": mode == "mute"})
                return {**result, "status": "ok", "muted": mode == "mute"}
            if not player.get("can_set_volume"):
                return {**result, "status": "unsupported"}
            if mode == "step" and player.get("volume") is None:
                return {**result, "status": "volume_unknown"}
            wanted = value if mode == "set" else player["volume"] + value
            level = max(0, min(wanted, 100, self.max_volume))
            await self._call("media_player", "volume_set", {
                "entity_id": player["entity_id"], "volume_level": level / 100})
            if player.get("muted") and player.get("can_mute") and value > 0:
                await self._call("media_player", "volume_mute", {
                    "entity_id": player["entity_id"], "is_volume_muted": False})
            return {**result, "status": "ok", "volume": level, "limited": wanted > self.max_volume}
        except MediaError as error:
            return {**result, "status": error.code}

    # ----- transport -------------------------------------------------------

    async def control(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request["operation"]
        if operation not in CONTROL_SERVICES:
            raise ValueError("unsupported control operation")
        players = self.catalog()
        context, override = self._source(request)
        if request.get("targets"):
            selection = resolve_targets(request["targets"], None, source=context,
                                        players=players, override=override)
            sessions = _unique([session_for(player, players) for player in selection["players"]])
        elif request.get("scope") in {"all", "playing"}:
            sessions = _active_sessions(players)
        else:
            origin = resolve_source(context, players, override)["player"]
            local = session_for(origin, players) if origin else None
            if local and (local.get("state") in ACTIVE_STATES or operation == "resume"):
                sessions = [local]
            else:
                # A satellite without its own active speaker controls the only
                # playing session; more than one needs a choice.
                active = _active_sessions(players)
                if len(active) > 1:
                    raise TargetError("ambiguous_target", "Music is playing on more than one speaker", active)
                if not active:
                    raise MediaError("not_playing", "No music is playing")
                sessions = active
        if not sessions:
            raise MediaError("not_playing", "No music is playing")
        results = []
        for session in sessions:
            try:
                await self._call("media_player", CONTROL_SERVICES[operation], {"entity_id": session["entity_id"]})
                results.append({**brief(session), "status": "ok"})
            except MediaError as error:
                results.append({**brief(session), "status": error.code})
        return _receipt({"players": results, "operation": operation})

    # ----- plumbing --------------------------------------------------------

    async def run(self, command: str, request_id: str, operation: Callable[[], Awaitable[dict]]) -> dict:
        """Run a mutation once per request ID; a duplicate gets the same result."""
        now = time.monotonic()
        while self._receipts and (len(self._receipts) > RECEIPT_LIMIT
                                  or now - next(iter(self._receipts.values()))[0] > RECEIPT_TTL):
            self._receipts.popitem(last=False)
        key = (command, request_id)
        if key in self._receipts:
            return await asyncio.shield(self._receipts[key][1])
        future = asyncio.get_running_loop().create_future()
        self._receipts[key] = (now, future)
        try:
            result = await operation()
        except asyncio.CancelledError:
            self._receipts.pop(key, None)
            future.cancel()
            raise
        except Exception as error:
            future.set_exception(error)
            future.exception()  # retrieved here; duplicates re-raise it
            raise
        future.set_result(result)
        return result

    async def _call(self, domain: str, service: str, data: dict, *, response: bool = False):
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                return await self.hass.services.async_call(
                    domain, service, data, blocking=True, return_response=response)
        except TimeoutError as error:
            raise MediaError("unknown", f"{domain}.{service} did not finish in time") from error
        except ServiceNotFound as error:
            raise MediaError("unavailable", f"{domain}.{service} is not available") from error
        except HomeAssistantError as error:
            _LOGGER.debug("%s.%s failed: %s", domain, service, error)
            raise MediaError("failed", str(error) or f"{domain}.{service} failed") from error

    async def _wait_state(self, entity_id: str, states: set[str], seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state in states:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.25)

    def playing_count(self) -> int:
        """Individual exposed Music Assistant speakers currently playing."""
        return sum(1 for player in self.catalog() if player.get("music_assistant")
                   and not player.get("is_group") and player.get("state") == "playing")


def public(player: dict[str, Any]) -> dict[str, Any]:
    """Catalog entry for Core; registry internals stay in HA."""
    hidden = {"unique_id", "config_entry_id", "device_id"}
    return {key: value for key, value in player.items() if key not in hidden}


def _active_sessions(players: list[dict]) -> list[dict]:
    return _unique([session_for(player, players) for player in players
                    if player.get("music_assistant") and player.get("state") in ACTIVE_STATES])


def _unique(items: list[dict | None]) -> list[dict]:
    seen, result = set(), []
    for item in items:
        if item is not None and item["entity_id"] not in seen:
            seen.add(item["entity_id"])
            result.append(item)
    return result


def _receipt(result: dict[str, Any]) -> dict[str, Any]:
    """Summarise per-speaker outcomes as completed, partial, failed or no_match."""
    players = result.get("players", [])
    succeeded = sum(item.get("status") == "ok" for item in players) + len(result.get("changed", []))
    failed = sum(item.get("status") != "ok" for item in players) + len(result.get("failed", []))
    if succeeded and not failed:
        status = "completed"
    elif succeeded:
        status = "partial"
    elif failed:
        status = "failed"
    else:
        # Nothing needed changing: already in the requested state, or no match.
        status = "completed" if result.get("already") else "no_match"
    return {"status": status, **result}


def _text(value: Any, field: str, *, required: bool = True) -> str | None:
    if not required and (value is None or (isinstance(value, str) and not value.strip())):
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{field} must be non-empty text of at most 200 characters")
    return value.strip()
