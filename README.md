# SmartHome Assistant for Home Assistant

One integration for the SmartHome HA Assist agent, local STT/TTS, durable jobs and HA-owned
timer delivery to compatible ESPHome Assist satellites. HA runs the voice pipeline; SmartHome Core interprets
requests and validates home actions. Core is installed separately.

## Install and update

Alarm/reminder management, expanded timer controls, HA-owned bulk cancellation and
explicit stop certainty require **1.7.0 or newer** and matching SmartHome Core.
The 1.6.1 release does not include these features. Check the
[published releases](https://github.com/dscrete/smarthome-ha/releases) before
updating: a version in a source manifest does not establish release availability.
The administrator **SmartHome** sidebar panel for timers and scheduled actions
is opt in. Enable **Show in sidebar** in the integration's **Configure** options.
The **Voice timers** sensor introduced in 1.5.0 requires compatible
SmartHome Core; update Core before or with an upgrade from an older version.
No beta selection is required in HACS. The software verification does not establish
physical satellite performance; the delivery limits below still apply.

Requires Home Assistant **2026.10.0 or newer**; 2026.10.0 is the tested version.
For that version install HACS, then use:

[Open SmartHome Assistant in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=dscrete&repository=smarthome-ha&category=integration)

Download the integration and restart HA, then:

[Add SmartHome Assistant](https://my.home-assistant.io/redirect/config_flow_start/?domain=smarthome_assistant)

Alternatively, add `https://github.com/dscrete/smarthome-ha` as an **Integration**
custom repository in HACS. HACS supplies HA update entities: install a released
update there and restart HA when requested. Updating integration code preserves
jobs in HA's separate persistent storage.

## Configure

1. Run SmartHome Core with its administrator HA connection configured. Leave
   the optional manual `CORE_HA_AGENT_TOKEN` empty. In this integration enter
   Core's reachable URL, leave **Manual Core credential** empty, then approve
   pairing and choose a **Persona**, or keep Core's configured default. This changes
   conversational wording; HA continues to own wake words and TTS voices.
2. Update the separate SmartHome Docker stack and fetch its saved voices with
   `git lfs pull --include="pocket-tts/voices/*"`. Version 1.4 starts the existing
   STT and Pocket TTS services plus an authenticated speech connector by default.
   In **Configure**, keep **Set up SmartHome speech platforms** enabled. The
   speech URL defaults to the Core host on port **8773**; set it explicitly for a
   reverse proxy or different host/port. It uses the already paired/manual
   credential, with no second token to copy. Only publish the connector to HA;
   keep the underlying STT/TTS service ports private.
3. Create a dedicated HA voice pipeline selecting **SmartHome Assistant** for
   conversation, speech-to-text and text-to-speech. Pick the saved TTS voice in
   HA. English is currently advertised for Pocket TTS; STT advertises its configured
   language. Other installed HA speech engines remain selectable: disable our
   speech-platform setup if they are preferred.
   **Required: turn Prefer handling commands locally off in this pipeline**, then
   assign the intended satellite. Otherwise HA can create native timers that bypass
   SmartHome's backend, validation and confirmation and do not appear in its jobs
   panel. SmartHome's own deterministic fast paths still handle simple supported
   commands without an LLM call. Existing native HA timers remain separate.
4. For EchoMuse, choose its **Home Assistant** voice backend and keep its normal
   controller online. HA's standard ESPHome integration supplies the connection.
5. Standard timer-capable ESPHome satellites, including unmodified EchoMuse, use
   their existing HA connection automatically. No custom controller action or
   output mapping is needed. See the recovery limits below.
   For optional enhanced EchoMuse delivery, open **Configure**. **Enhanced timer
   output mapping** maps each HA satellite to its advertised exchange action:

   ```json
   {
     "assist_satellite.kitchen": "esphome.echomuse_0123456789ab_smarthome_timer_exchange_v1"
   }
   ```

   Enhanced delivery requires the compatible fork controller. The old Gateway
   binding field is retired; any saved legacy values are ignored.
6. Configure a TTS voice in HA's preferred Assist pipeline for timer names.
   Missing or disconnected outputs reject a new timer before persistence.

In **Configure**, optionally set **Default alert satellite** for requests without a
source satellite, and **Default snooze duration (seconds)** (540 by default).
Explicit output selection and the requesting satellite take precedence. Saved
alerts retain their output identity; changing the default does not reroute them.

The SmartHome panel supports timer creation, pause/resume, time adjustment, rename
and restart, plus alarm/reminder creation, recurrence and editing. Alarm snooze or
dismiss affects one occurrence; enable/disable and cancellation affect the saved
schedule. These controls and accepted alert delivery work with Core stopped.
Daily, weekly, monthly and interval schedules use HA's saved timezone and recovery
rules. Future alarms/reminders may target a temporarily disconnected registered
output; new timers still require a connected output. Reminder speech requires an
announcement-capable satellite and a configured HA TTS provider. No model is called
when the reminder becomes due. Standard alarm stop/snooze requests cannot confirm
silence; the panel distinguishes them from acknowledged enhanced delivery.

Core's own HA token needs administrator access for catalog metadata and job
commands. Configure that token in Core; it is separate from this integration's
bearer credential. Keep credentials out of repository files and URLs.

Speech engines appear once the connector and models are ready. A speech outage
does not disable the conversation agent; HA retries unavailable speech platforms.
On a nonstandard deployment, check **Configure → Speech service URL** and that HA
can reach that URL. Selecting a conversation agent alone never selects its STT/TTS.
If STT or TTS remains absent, inspect `docker compose logs speech-bridge stt tts`.
Do not install Wyoming to connect these HTTP services: the integration registers
native HA speech entities directly.

The connector reads Core's existing pairing hash from a read-only mount. Pairing
rotation/revocation applies to speech too. Core does not relay speech; stopping Core
does not stop the connector or engines, including timer-name synthesis. Models stay
on the separate SmartHome host; the HA integration does not install them.

### SmartHome panel

In **Settings → Devices & services → SmartHome Assistant → Configure**, enable
**Show in sidebar**. The default is off, including when upgrading from 1.6.0.
Open **SmartHome** in Home Assistant's sidebar as an administrator to see active
assistant timers and scheduled actions, with countdowns, due times and retained
outcomes. The integration registers the panel when enabled; no dashboard YAML,
frontend resource installation or separate web service is needed. It uses HA's
built-in frontend and custom-panel support with locally bundled JavaScript.

Pending or due jobs have **Cancel** controls; sounding timers have **Dismiss**.
Changes from voice requests and other browsers update the view. If a job changes
before a click reaches HA, the panel refreshes and reports the conflict. A lost
response can leave the outcome unknown; check the refreshed record before making
another request. The panel does not automatically repeat mutations.

Standard ESPHome timer handoff cannot confirm playback or local dismissal. Those
records show the handoff and its limits, rather than a confirmed ringing state or
an unsupported dismiss button. Enhanced outputs can expose an acknowledged
sounding state and its supported dismissal operation.

Turning **Show in sidebar** off removes the panel without changing jobs or timer
delivery. The preference is retained when other integration settings change.
The enabled panel shares the existing job API's administrator restriction and stays
available when Core is offline or the conversation entry is unloaded. Reloading
the agent does not cancel jobs. Scheduled device actions still require Core when
due. The overview covers assistant-owned jobs, not native HA timer helpers.
Alarm/reminder definitions and their individual occurrences appear separately,
so dismissing an occurrence leaves its recurring definition intact. See
[the job API](custom_components/smarthome_assistant/jobs.md).

### Voice timers sensor

The integration adds count-only **Voice timers**, **Alarms** and **Reminders**
sensors. Voice timers counts pending, paused, due or sounding timers; the other
sensors count enabled definitions. They carry no timer names or saved messages,
so nothing personal enters HA history. Their purpose is retrieval:
SmartHome Core hides these sensors from the language model and, when a request semantically
matches one, offers the corresponding assistant capability instead. This lets mis-heard requests such
as "What times do I have?" still reach timers.

When HA first creates each sensor, the integration sets generic aliases such as
"timers" and "countdown timers" and exposes it to Assist, because SmartHome only indexes exposed
entities. It does this once: later changes you make to its aliases or exposure are
kept. If you unexpose it, timers still work, but only requests containing timer
words (timer, countdown, seconds, minutes, hours, duration) reach them. Core
matches the sensor by integration and registry unique ID, so renaming its entity ID
is safe. Core versions before this feature show it to the model as an ordinary
read-only sensor.

### Pairing and connection security

Automatic pairing creates the separate Core credential for you; entering the Core
URL is still required. HA opens a five-minute setup request. Core sends its offer
back through its configured, administrator-authenticated HA connection. Only your
approval activates it. Other LAN clients cannot obtain a credential by calling the
pairing endpoint. Manual credentials remain supported: set the same dedicated
secret in Core's `CORE_HA_AGENT_TOKEN` and HA's optional manual field instead.
A nonempty manual override disables automatic pairing.

HTTP is supported on a trusted home network, as is HTTPS with normal certificate
verification. **HTTP does not encrypt credentials or protect them from interception.**
Use HTTPS outside that trusted network. Never put a credential in a URL.

Reconfigure the integration and approve pairing again to replace an automatic
credential. The previous credential stops working only after approval. Removing
the integration revokes its automatic credential when Core is reachable; this
does not delete timers or jobs. Offline removal cannot revoke the remote key.
For recovery, stop Core, delete only `data/ha-agent/pairing.json` in Core's data
folder, restart and pair again. Preserve `requests.db` and HA job storage. Core
stores only the credential hash in that file; HA stores the actual credential in
its private config entry. Protect both systems' data and backups.

An aborted request grants no access. An abandoned Core proposal expires within
five minutes; restart Core to clear it sooner. If approval times out, Core may
already have saved the credential. Resubmit the approval deliberately while the
HA form is active, or start pairing again after expiry. Setup never retries home
actions or changes to another conversation engine.

## Dependencies and limits

- **SmartHome Core** supplies conversation/action interpretation. This package
  does not install Core, an LLM provider or speech models.
- EchoMuse Dots need their **normal controller**. Standard delivery works through
  its native timer support. Optional enhanced acknowledgement/recovery requires
  the compatible private fork at
  `d5639cc3fd97da08da7df72504f982638d82e5d8`:
  [EchoMuse change](https://github.com/DylanSchwikkard/EchoMuse/pull/16).
  An ordinary upstream controller does not supply that optional exchange.
- Accepted new timers need HA and that controller, with no Core/Gateway dependency
  for expiry or local dismissal. Finish or cancel active legacy Gateway timers
  with the previous Core version before upgrading; the HA-only Core refuses them.
- Scheduled device actions still require Core at dispatch. Missed actions are
  skipped; uncertain actions are never automatically replayed.
- Timer delivery is software-tested with synthetic Dots. Physical audio, acoustic
  wake recognition and display remain unverified. Music expansion is outside
  this release.

### Standard timer recovery limits

HA retains the only durable countdown. It sends ordinary start/update/cancel/finish
events to the registered satellite; it does not create a second Assist timer.
Core and Gateway can stop after creation. Pending jobs survive HA restarts and
may wait for their original output for up to five minutes after due time.

A finish event is reserved durably and sent at most once. HA records **handed off**,
not confirmed playback or local dismissal. After an uncertain interruption it
reports uncertainty and does not replay the alert. This prevents a dismissed alert
from being resurrected, but may lose an alert interrupted during handoff. At the
original five-minute cutoff HA sends cancellation when connected; a disconnected
satellite can continue according to its own firmware. Local button/wake behaviour
also belongs to the satellite. Enhanced delivery provides the stronger acknowledged
contract; standard devices are not claimed to have identical recovery behaviour.
In the tested upstream EchoMuse revision, wake-and-speech dismissal silences all
ringing Dots on that controller; its button dismissal is local. SmartHome does
not change upstream's wake handling. Review this limitation before using multiple
Dots for simultaneous timers.

The standard adapter uses HA's existing ESPHome connection and checks timer
capability and exact HA device identity. HA 2026.10 has no public dispatcher for
externally owned timers, so one isolated adapter reads its registered internal
timer handler. It checks the API shape and fails closed when incompatible; HA
upgrades need compatibility verification. Alarms reuse this presentation path;
spoken reminders instead require HA announcement support and a configured TTS pipeline.

## Migration from two installed folders

Back up HA. Keep the existing **SmartHome Assistant** config entry and pipeline
references. Transfer any old `smarthome_jobs.timer_outputs` YAML mapping into this
integration's timer options. Remove the `smarthome_jobs:` YAML block and retire
`custom_components/smarthome_jobs` before restarting into the combined version.
Only `custom_components/smarthome_assistant` is installed going forward.

For the HA-only Core update, first finish/cancel active Gateway timers using the
previous version. Their IDs are listed in HA's migration notification. Stored
owners are preserved; no timer is silently moved to another delivery path. Stop
the old SmartHome Compose project before updating so an orphan Gateway container
does not remain active. Preserve bind-mounted data and volumes. Persona changes
apply through **Configure**; omitted selections retain Core's default.

Do **not** remove `.storage/smarthome_jobs.storage`: its key, job IDs, HA authority,
delivery tokens and API are preserved. Finish or cancel active timers before
changing their output binding. Conversation entry reloads leave delivery running;
disabling/removing the whole integration can stop it after HA restarts.

The expanded scheduling update migrates the job document to version 4, preserving
existing jobs, delivery ownership, protocol bindings and request receipts, and
adding recurring definitions. Back up HA before updating. Earlier releases cannot
read this document; a deliberate downgrade needs the corresponding backup,
rather than removing the store or changing active timers' owners.

This repository is exported from the private development project without its Git
history, credentials, household configuration or test data. See the included
[job API](custom_components/smarthome_assistant/jobs.md) for protocol details.
