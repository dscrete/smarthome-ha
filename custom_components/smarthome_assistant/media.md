# SmartHome music API

Music playback and speaker grouping for SmartHome Core, built on
[Music Assistant](https://www.home-assistant.io/integrations/music_assistant).
Core's `assistant.music` capability sends **slots only** (kind, query, artist,
speaker). HA resolves speakers, searches Music Assistant, chooses a result
deterministically, calls HA's existing `music_assistant.*` and `media_player.*`
actions and returns what happened. The model never chooses a URI.

Volume, pause/resume, next/previous, stop and "what's playing" are not part of
this API: Core handles them through its generic `media_player` path on the same
exposed Music Assistant players.

Both commands use HA's authenticated WebSocket API and require an
administrator, like the [job API](jobs.md). They take a `request_id`; resending
the same ID within ten minutes returns the original result instead of acting
again. The receipt cache is in memory and does not survive an HA restart; Core's
request journal remains the authority on replays.

Nothing is configured per household. The speaker catalog is every
**Assist-exposed `media_player`**, described by its HA name, aliases, area and
Music Assistant metadata. **Configure → Speaker overrides**
(`{assist_satellite.x: media_player.y}`) is only for a satellite whose automatic
speaker is wrong.

## Home setup

1. Expose the Music Assistant player entities to Assist and assign each an HA
   area. Exposing the underlying ESPHome player as well is harmless: name and
   room matches prefer the Music Assistant player for the same speaker.
2. Expose your saved Music Assistant group player and give it aliases such as
   `everywhere` and `whole house`. That group is what "everywhere" means.
3. Radio mode (an endless mix after a song or artist) needs a provider with
   similar-track support, such as Spotify.

## Source speaker and sessions

Commands accept `source_satellite` (the requesting `assist_satellite` entity)
and/or `device_id`. The speaker you are talking to is:

1. the configured override for that satellite; otherwise
2. the individual Music Assistant player that is the same physical speaker:
   same HA device, the MA player wrapping an entity of the satellite's device,
   the satellite device's MAC within the MA player ID, or the exact same device
   name; otherwise
3. the only individual Music Assistant player in the satellite's area.

Anything else is `no_target`: ask which speaker. Resolution never guesses.

A speaker's **session** is what owns its playback: an active saved group that
contains it, else the leader of its dynamic sync group (HA lists the leader
first), else the speaker itself. "Play X" on a Dot that is playing as part of a
group plays X on the whole group.

## Targets

Commands take either `targets` (up to 16 strings) or `scope`:

- `targets`: exact entity IDs, exact names/aliases, or exact area names/IDs,
  compared case-insensitively without punctuation or a leading "the". A name
  matching several speakers is `ambiguous_target` with `choices`; a name matching
  none is `speaker_not_found`. An area returns every speaker in it.
- `scope`: `source` (default) or `all` (everywhere).

## `smarthome_assistant/media/play`

`{request_id, kind, query, artist?, enqueue?, radio_mode?, targets?|scope?,
source_satellite?, device_id?}`

- `kind`: `track | artist | album | playlist | radio`.
- `query`: the spoken title or name. Put an artist in `artist`. If
  "<title> by <artist>" arrives together, HA first scores that reading against
  the results, and searches the split if the whole phrase finds nothing.
- `enqueue`: `replace` (default), `play`, `next`, `add`.
- `radio_mode`: defaults to on for `track` and `artist` and off otherwise. If
  Music Assistant rejects radio mode, HA plays the item once without it and
  reports `mix_unavailable: true`.
- Radio stations search Music Assistant library favourites first, then providers.
- `scope: all` plays on the "everywhere" group; several targets play on the
  first (or the source speaker) and join the rest.

Result `status`:

- `playing`: the speaker reported playing within four seconds.
- `requested`: accepted but not yet confirmed playing.
- `unknown`: the play call timed out; it may have started. Do not retry.
- `not_found`, optionally with a `suggestion` (the closest item).

Searching is bounded to 12 seconds and fails as `unavailable` before anything
plays. Successful results carry `item {name, artists, album, version,
media_type, uri}`, `mix`, `target`, `joined` and `not_joined`.

## `smarthome_assistant/media/speakers`

`{request_id, operation, targets?|scope?, session?, source_satellite?}`

- `add`: join speakers to the current session ("also play in the kitchen").
  `scope: all` moves playback to the "everywhere" group.
- `remove`: take speakers out. Removing a dynamic group's leader hands its queue
  to a remaining speaker; removing every speaker stops the music.
- `move`: transfer the queue to one speaker or group ("move it to the lounge").
- `only`: keep just these speakers ("just the kitchen").

The session is the source speaker's active session, else the only active session
in the home; `session` names it explicitly. Several active sessions without a
choice return `ambiguous_target`, and none returns `not_playing`. Results list
`changed`, `failed` (with `reason`), `already` and `not_in_session`, plus
`status` `completed | partial | failed | no_match`.

## Statuses

Expected outcomes are results, not WebSocket errors: `no_target`,
`ambiguous_target` (with `choices`), `speaker_not_found`, `not_found` (no such
music), `not_playing`, `unsupported`,
`unavailable` (Music Assistant not loaded, search incomplete or speaker offline),
`failed` and `unknown`. Malformed requests are WebSocket errors with code
`invalid_format`. There are no automatic retries except the single radio-mode
fallback.

The count-only **Music** sensor (unique ID suffix `_music`, aliases `music,
songs, radio station, speakers`) counts exposed Music Assistant speakers
playing. Core shadows it to promote `assistant.music` in retrieval.
