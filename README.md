# SmartHome Assistant for Home Assistant

One integration for the SmartHome HA Assist agent, durable jobs and HA-owned
EchoMuse timer delivery. HA runs the voice pipeline; SmartHome Core interprets
requests and validates home actions. Core is installed separately.

## Install and update

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

1. Run SmartHome Core and configure its dedicated HA-agent bearer credential.
   Enter Core's reachable URL and that credential in this integration.
2. Create a dedicated HA voice pipeline selecting **SmartHome Assistant** and
   your STT/TTS. Disable **Prefer handling commands locally** so native intents
   cannot bypass Core's backend and validation. Assign the intended satellite.
3. For EchoMuse, choose its **Home Assistant** voice backend and keep its normal
   controller online. HA's standard ESPHome integration supplies the connection.
4. For timers, open this integration's **Configure** options. **Timer output
   mapping** maps each HA satellite to its advertised ESPHome action, for example:

   ```json
   {
     "assist_satellite.kitchen": "esphome.echomuse_0123456789ab_smarthome_timer_exchange_v1"
   }
   ```

   Use the action advertised by the compatible controller. The separate gateway
   bindings are for legacy Gateway features and can stay empty for this HA route.
5. Configure a TTS voice in HA's preferred Assist pipeline for timer names.
   Missing or disconnected outputs reject a new timer before persistence.

Core's own HA token needs administrator access for catalog metadata and job
commands. Configure that token in Core; it is separate from this integration's
bearer credential. Keep credentials out of repository files and URLs.

## Dependencies and limits

- **SmartHome Core** supplies conversation/action interpretation. This package
  does not install Core, an LLM provider or speech models.
- EchoMuse Dots need their **normal controller**. HA-owned timer output currently
  requires the compatible private fork at
  `d5639cc3fd97da08da7df72504f982638d82e5d8`:
  [EchoMuse change](https://github.com/DylanSchwikkard/EchoMuse/pull/16).
  An older published controller image does not supply the exchange.
- Accepted new timers need HA and that controller, with no Core/Gateway dependency
  for expiry or local dismissal. Legacy timers retain their original owner.
- Scheduled device actions still require Core at dispatch. Missed actions are
  skipped; uncertain actions are never automatically replayed.
- Timer delivery is software-tested with synthetic Dots. Physical audio, acoustic
  wake recognition and display remain unverified. Music expansion, reminders and
  recurring alarms are outside this release.

## Migration from two installed folders

Back up HA. Keep the existing **SmartHome Assistant** config entry and pipeline
references. Transfer any old `smarthome_jobs.timer_outputs` YAML mapping into this
integration's timer options. Remove the `smarthome_jobs:` YAML block and retire
`custom_components/smarthome_jobs` before restarting into the combined version.
Only `custom_components/smarthome_assistant` is installed going forward.

Do **not** remove `.storage/smarthome_jobs.storage`: its key, job IDs, HA authority,
delivery tokens and API are preserved. Finish or cancel active timers before
changing their output binding. Conversation entry reloads leave delivery running;
disabling/removing the whole integration can stop it after HA restarts.

This repository is exported from the private development project without its Git
history, credentials, household configuration or test data. See the included
[job API](custom_components/smarthome_assistant/jobs.md) for protocol details.
