# SmartHome music and volume API

Music, speaker grouping, volume and transport for SmartHome Core, built on
[Music Assistant](https://www.home-assistant.io/integrations/music_assistant).
Core interprets speech and sends **slots only**. HA resolves speakers, searches
Music Assistant, chooses a result deterministically, calls HA's existing
`music_assistant.*` and `media_player.*` actions and returns what happened.
The model never chooses a URI or an entity it was not given.

All commands use HA's authenticated WebSocket API and require an administrator,
like the [job API](jobs.md). Mutations take a `request_id`; resending the same ID
within ten minutes returns the original result instead of acting again (the
receipt cache is in memory and does not survive an HA restart). Core's request
journal remains the authority on replays.

Nothing is configured per household. The speaker catalog is every
**Assist-exposed `media_player`**, described by its HA name, aliases, area and
Music Assistant metadata. Optional options in **Configure**:

- **Speaker overrides** `{assist_satellite.x: media_player.y}`: only when
  automatic source resolution picks the wrong speaker.
- **Maximum voice volume** (1–100 %, default 100): ceiling for voice set/step.

## Home setup

1. Expose the Music Assistant player entities to Assist and assign each an HA
   area. Exposing the underlying ESPHome player as well is harmless: name and
   room matches prefer the Music Assistant player for the same speaker.
2. Expose your saved Music Assistant group player and give it aliases such as
   `everywhere` and `whole house`. That group is what "everywhere" means.
3. Radio mode (endless mixes after a song or artist) needs a provider with
   similar-track support, such as Spotify.

## Source speaker and sessions

Every command accepts `source_satellite` (the requesting `assist_satellite`
entity from the turn) and/or `device_id`. The speaker you are talking to is:

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
  matching several speakers is `ambiguous_target` with `choices`. An area
  returns every speaker in it.
- `scope`: `source` (default), `all` (everywhere), or `playing`.

## Commands

### `smarthome_assistant/media/info`

`{source_satellite?, device_id?}` → `{version: 1, available, max_volume,
players: [...], source: {player, reason, session}}`. Each player has
`entity_id, name, aliases, device_name, area_id, area_name, music_assistant,
is_group, members, state, available, volume (0–100|null), muted,
can_set_volume, can_mute, can_group, now_playing {title, artist, album_name}`.
Use it for "what's playing?" and to check `available` before offering music.

### `smarthome_assistant/media/play`

`{request_id, kind, query, artist?, enqueue?, radio_mode?, targets?|scope?,
source_satellite?, device_id?}`

- `kind`: `track | artist | album | playlist | radio`.
- `query`: the spoken title/name. Put an artist in `artist`; if Core leaves
  "<title> by <artist>" together, HA retries with that split after the whole
  phrase fails.
- `enqueue`: `replace` (default), `play`, `next`, `add`.
- `radio_mode`: defaults to on for `track` and `artist` (Alexa-style endless
  mix) and off otherwise. If Music Assistant rejects radio mode, HA plays the
  item without it and reports `mix_unavailable: true`.
- Radio stations search Music Assistant library favourites first, then providers.
- `scope: all` plays on the "everywhere" group; several targets play on the
  first (or the source speaker) and join the rest.

Result `status`:

- `playing`: the speaker reported playing within six seconds.
- `requested`: accepted but not yet confirmed playing.
- `unknown`: the play call timed out; it may have started. Do not retry.
- `not_found`, optionally with `suggestion` ("Did you mean ...?").

Successful results carry `item {name, artists, album, version, media_type,
uri}`, `mix`, `target`, `joined` and `not_joined`. Say the chosen item ("Playing
Hello by Adele, then similar songs") so the person can correct it, rather than
asking first.

### `smarthome_assistant/media/speakers`

`{request_id, operation, targets?|scope?, session?, source_satellite?}`

- `add`: join speakers to the current session ("also play in the kitchen").
  `scope: all` moves playback to the "everywhere" group.
- `remove`: take speakers out. Removing a dynamic group's leader hands its queue
  to a remaining speaker; removing every speaker stops the music.
- `move`: transfer the queue to one speaker or group ("move it to the lounge").
- `only`: keep just these speakers ("just the kitchen").

The session is the source speaker's active session, else the only active session
in the home; `session` names it explicitly. Several active sessions without a
choice return `ambiguous_target`. Results list `changed`, `failed` (with
`reason`), `already` and `not_in_session`, plus `status` `completed | partial |
failed | no_match`.

### `smarthome_assistant/media/volume`

`{request_id, mode, value?, targets?|scope?, source_satellite?}`

- `mode`: `set` (value 0–100), `step` (value −100–100), `mute`, `unmute`.
  Core maps "volume 4" on the 0–10 scale to 40, and "a little / a lot" to step
  sizes (for example 5 / 10 / 20).
- Works on any exposed `media_player` that supports volume, not only Music
  Assistant (so "turn the TV down" works).
- No target: only the speaker you are talking to, even when it is in a group.
  `scope: all`: every speaker in the source's group (or every playing speaker).
  A group target expands to its members, and each member steps from its own level.
- Levels are clamped to 0 and the configured maximum (`limited: true`).
  Raising the volume of a muted speaker unmutes it.

Results list each speaker's `previous`, `volume` and `status` (`ok`,
`unsupported`, `volume_unknown`, `failed`, `unknown`).

### `smarthome_assistant/media/control`

`{request_id, operation, targets?|scope?, source_satellite?}` with `operation`
`pause | resume | stop | next | previous`. With no target this acts on the source
speaker's session. Otherwise it acts on the only active session; several active
sessions return `ambiguous_target`, and none returns `not_playing`. `scope:
all|playing` acts on every active session.

## Statuses common to all commands

Expected outcomes are results, not WebSocket errors: `no_target`,
`ambiguous_target` (with `choices`), `not_found`, `not_playing`, `unsupported`,
`unavailable` (Music Assistant not loaded or speaker offline), `failed` and
`unknown`. Malformed requests are WebSocket errors with code `invalid_format`.
There are no automatic retries except the single radio-mode fallback.

## Core guidance

- **Stop and dismiss precedence:** if an alarm or timer is sounding for the
  source, "stop" belongs to the alert, not the music.
- **Fast paths:** volume up/down/set/mute and pause/resume/next/previous need no
  model call.
- **Retrieval anchor:** the count-only **Music** sensor (unique ID suffix
  `_music`, aliases `music, songs, radio station, speakers, volume`) counts
  exposed Music Assistant speakers playing. Shadow it, and Music Assistant
  `media_player` entities, to this contract the same way as the timer sensors.
- Music Assistant players are low-risk targets: no confirmation is required.
