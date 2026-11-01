"""Pure speaker resolution for SmartHome music and volume commands.

Works on plain player descriptions built by ``media.py`` from HA's registries,
so the rules can be unit tested without Home Assistant. Nothing here is
configured per household: the catalog is every Assist-exposed media player,
described by its own HA name, aliases, area and Music Assistant metadata.
Resolution fails closed; it never guesses a speaker.
"""
from __future__ import annotations

from typing import Any, Iterable

from .media_match import normalize

ACTIVE_STATES = ("playing", "paused", "buffering")


class TargetError(Exception):
    """A target could not be resolved to exactly one meaning."""

    def __init__(self, code: str, message: str, choices: Iterable[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.choices = [brief(item) for item in choices or ()]


def brief(player: dict[str, Any]) -> dict[str, Any]:
    """Identity Core may speak or send back as an exact target."""
    return {"entity_id": player["entity_id"], "name": player.get("name"),
            "area_name": player.get("area_name"), "is_group": bool(player.get("is_group"))}


def _hex(value: Any) -> str:
    return "".join(char for char in str(value or "").lower() if char in "0123456789abcdef")


def link_reason(player: dict[str, Any], source: dict[str, Any]) -> str | None:
    """Return how an MA player is the same physical speaker as the source device."""
    if not player.get("music_assistant") or player.get("is_group"):
        return None
    device_id = source.get("device_id")
    if device_id and player.get("device_id") == device_id:
        return "device"
    # MA's Home Assistant player provider uses the wrapped entity ID as its
    # player ID, which HA stores as the MA entity's unique ID.
    if player.get("unique_id") and player["unique_id"] in set(source.get("device_entity_ids") or ()):
        return "wrapped_entity"
    mac = source.get("mac")
    if mac and len(mac) == 12 and mac in _hex(player.get("unique_id")):
        return "mac"
    source_name = normalize(source.get("device_name"))
    if source_name and source_name in {normalize(player.get("device_name")), normalize(player.get("name"))}:
        return "device_name"
    return None


def resolve_source(source: dict[str, Any], players: list[dict[str, Any]],
                   override: str | None = None) -> dict[str, Any]:
    """Resolve the speaker the person is talking to.

    Order: configured override, then an MA player that is the same physical
    device (device, wrapped entity, MAC, exact device name), then the only
    individual MA player in the satellite's area. Anything else is no target.
    """
    by_id = {player["entity_id"]: player for player in players}
    if override:
        player = by_id.get(override)
        return {"player": player, "reason": "override" if player else "override_unavailable"}
    individual = [player for player in players
                  if player.get("music_assistant") and not player.get("is_group")]
    for reason in ("device", "wrapped_entity", "mac", "device_name"):
        matches = [player for player in individual if link_reason(player, source) == reason]
        if len(matches) == 1:
            return {"player": matches[0], "reason": reason}
        if len(matches) > 1:
            break
    area_id = source.get("area_id")
    if area_id:
        in_area = [player for player in individual if player.get("area_id") == area_id]
        if len(in_area) == 1:
            return {"player": in_area[0], "reason": "area"}
    return {"player": None, "reason": "none"}


def session_for(player: dict[str, Any] | None, players: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the player that owns this speaker's current playback.

    A member of an active saved group plays as that group. A member of a
    dynamic sync group plays through its leader (HA lists the leader first).
    """
    if player is None or not player.get("music_assistant"):
        return player
    if player.get("is_group"):
        return player
    groups = [group for group in players if group.get("is_group")
              and player["entity_id"] in (group.get("members") or ())
              and group.get("state") in ACTIVE_STATES]
    playing = [group for group in groups if group.get("state") == "playing"]
    if len(playing) == 1:
        return playing[0]
    if len(groups) == 1:
        return groups[0]
    members = player.get("members") or []
    if len(members) > 1 and members[0] != player["entity_id"]:
        leader = next((item for item in players if item["entity_id"] == members[0]), None)
        if leader is not None:
            return leader
    return player


def everywhere_group(source_player: dict[str, Any] | None,
                     players: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the saved MA group that means "everywhere" for this source.

    The largest exposed group containing the source wins; without a source
    match, a household with exactly one exposed group uses it.
    """
    groups = [player for player in players if player.get("music_assistant") and player.get("is_group")]
    if source_player is not None:
        containing = [group for group in groups
                      if source_player["entity_id"] in (group.get("members") or ())]
        if containing:
            size = max(len(group.get("members") or ()) for group in containing)
            largest = [group for group in containing if len(group.get("members") or ()) == size]
            if len(largest) == 1:
                return largest[0]
            raise TargetError("ambiguous_target", "More than one speaker group includes this speaker", largest)
    if len(groups) == 1:
        return groups[0]
    if len(groups) > 1:
        raise TargetError("ambiguous_target", "More than one speaker group could mean everywhere", groups)
    return None


def members_of(player: dict[str, Any], players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Physical speakers behind a target: a group's members, else itself."""
    if not player.get("is_group") and len(player.get("members") or ()) <= 1:
        return [player]
    by_id = {item["entity_id"]: item for item in players}
    found = [by_id[entity_id] for entity_id in player.get("members") or () if entity_id in by_id]
    return found or [player]


def expand_groups(player: dict[str, Any], players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Physical speakers a named target means: a saved group's members, else itself.

    Unlike ``members_of``, naming one speaker that leads a dynamic sync group
    means only that speaker.
    """
    return members_of(player, players) if player.get("is_group") else [player]


def resolve_name(name: str, players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve one spoken or exact target.

    Exact entity ID, then exact name/alias, then exact area. An area may name
    several speakers; Core decides whether that is acceptable (volume) or
    needs a choice (playback).
    """
    if not isinstance(name, str) or not name.strip():
        raise TargetError("invalid_target", "A target must be a non-empty string")
    raw = name.strip().casefold()
    exact = [player for player in players if player["entity_id"].casefold() == raw]
    if exact:
        return exact
    requested = normalize(name)
    named = [player for player in players if requested and requested in
             {normalize(player.get("name")), *(normalize(alias) for alias in player.get("aliases") or ())}]
    named = _prefer_music_assistant(named)
    if named:
        if len(named) > 1:
            raise TargetError("ambiguous_target", "More than one speaker has that name", named)
        return named
    in_area = [player for player in players if requested and requested in
               {normalize(player.get("area_name")), normalize(player.get("area_id"))}]
    in_area = _prefer_music_assistant([player for player in in_area if not player.get("is_group")])
    if not in_area:
        # Distinct from a missing song: a named speaker or room that does not exist.
        raise TargetError("speaker_not_found", "No exposed speaker matches that name or room")
    return in_area


def _prefer_music_assistant(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop a raw device player when its Music Assistant player also matched.

    Exposing both the ESPHome player and its MA wrapper should not make the
    same physical speaker ambiguous.
    """
    music = [player for player in candidates if player.get("music_assistant")]
    if not music:
        return candidates
    kept = list(music)
    for player in candidates:
        if player.get("music_assistant"):
            continue
        source = {"device_id": player.get("device_id"), "device_entity_ids": [player["entity_id"]],
                  "device_name": player.get("device_name")}
        if not any(link_reason(item, source) for item in music):
            kept.append(player)
    return kept


def resolve_targets(names: list[str] | None, scope: str | None, *, source: dict[str, Any],
                    players: list[dict[str, Any]], override: str | None = None) -> dict[str, Any]:
    """Resolve a command's target selection.

    Returns ``{"source": <source resolution>, "players": [..], "scope": str}``.
    Named targets win over scope. ``source`` (the default) is the speaker the
    person is talking to; ``all`` is expanded by callers that know which group
    or set of speakers "everywhere" means for their operation.
    """
    origin = resolve_source(source, players, override)
    if names:
        found: list[dict[str, Any]] = []
        for name in names:
            for player in resolve_name(name, players):
                if player["entity_id"] not in {item["entity_id"] for item in found}:
                    found.append(player)
        return {"source": origin, "players": found, "scope": "named"}
    scope = scope or "source"
    if scope not in {"source", "all"}:
        raise TargetError("invalid_target", "Scope must be source or all")
    if scope == "source":
        if origin["player"] is None:
            raise TargetError("no_target", "This satellite has no speaker; ask which speaker to use")
        return {"source": origin, "players": [origin["player"]], "scope": scope}
    return {"source": origin, "players": [], "scope": scope}
