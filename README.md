# SmartHome Assistant for Home Assistant

One integration for the SmartHome HA Assist agent, durable jobs and HA-owned
timer delivery to compatible ESPHome Assist satellites. HA runs the voice pipeline; SmartHome Core interprets
requests and validates home actions. Core is installed separately.

## Install and update

The current stable release is **1.3.0**, with the same runtime as 1.3.0 beta 4.
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
2. Create a dedicated HA voice pipeline selecting **SmartHome Assistant** and
   your STT/TTS. Disable **Prefer handling commands locally** so native intents
   cannot bypass Core's backend and validation. Assign the intended satellite.
3. For EchoMuse, choose its **Home Assistant** voice backend and keep its normal
   controller online. HA's standard ESPHome integration supplies the connection.
4. Standard timer-capable ESPHome satellites, including unmodified EchoMuse, use
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
5. Configure a TTS voice in HA's preferred Assist pipeline for timer names.
   Missing or disconnected outputs reject a new timer before persistence.

Core's own HA token needs administrator access for catalog metadata and job
commands. Configure that token in Core; it is separate from this integration's
bearer credential. Keep credentials out of repository files and URLs.

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
  wake recognition and display remain unverified. Music expansion, reminders and
  recurring alarms are outside this release.

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
upgrades need compatibility verification. Alarms/reminders remain future features.

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

The portable-timer update migrates the job document to version 3, preserving
existing delivery ownership. Back up HA before updating. Version 1.2 cannot read
that document; a deliberate downgrade needs the corresponding backup, rather
than removing the store or changing active timers' owners.

This repository is exported from the private development project without its Git
history, credentials, household configuration or test data. See the included
[job API](custom_components/smarthome_assistant/jobs.md) for protocol details.
