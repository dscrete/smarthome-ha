# SmartHome Assistant for HA Assist

This project-owned integration is opt-in. It exposes conversation, STT and TTS entities;
Home Assistant runs the pipeline and follow-up listening. The paired speech connector
calls our existing STT/Pocket TTS engines. Core owns final-text
interpretation, validation, exact-target execution, confirmation and trusted replies.
The integration does not install a model or change other pipelines.

## Install and select

1. Install [SmartHome Assistant through HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=dscrete&repository=smarthome-ha&category=integration) and restart HA.
   Released updates use HACS-provided HA update entities. Developer installs can
   still copy this one folder into `config/custom_components/smarthome_assistant`.
2. Configure Core's administrator HA connection. Leave **Manual HA agent
   credential** (`CORE_HA_AGENT_TOKEN`) empty for automatic pairing. Existing
   nonempty manual credentials continue to work and disable automatic pairing.
3. In HA → Settings → Devices & services → Add integration, select **SmartHome
   Assistant**. Enter Core's reachable HTTP(S) URL, leave the optional manual
   credential empty, then approve pairing. HTTP is allowed on a trusted home
   network but does not encrypt credentials; use HTTPS outside it. Never put
   credentials in the URL. Choose a **Persona**, or keep Core's default. This is a
   wording style only; configure wake word and TTS voice in HA/the satellite.
4. Update the separate SmartHome stack and fetch its LFS-backed voices. Keep
   **Set up SmartHome speech platforms** enabled in Configure; the paired speech
   connector defaults to Core's host on port 8773. Set **Speech service URL** for
   a different host/port or reverse proxy. Create a separate HA voice pipeline,
   selecting **SmartHome Assistant** for conversation, STT and TTS, then choose a
   TTS voice. Existing HA engines can still be selected instead. See the full
   [speech setup](https://github.com/dscrete/smarthome-ha#configure).
   **Disable “Prefer handling
   commands locally”**. Otherwise native HA intents, including timers, can bypass
   Core's backend, validation and confirmation.
5. Assign only the intended test satellite to that pipeline. In EchoMuse,
   choose its **Home Assistant voice backend** when using this pipeline.
   Physical EchoMuse playback remains unverified until an isolated hardware test.
6. Standard timer-capable ESPHome Assist satellites need no output mapping.
   For optional enhanced EchoMuse recovery, enter its exchange mapping in
   Configure, following [installation and migration](https://github.com/dscrete/smarthome-ha).
   No separate jobs folder or YAML block is needed. The old Gateway-binding field
   is removed; saved legacy values are ignored.

The `smarthome_jobs` backend is included in this integration for timers and
schedules. To enable the administrator-only **SmartHome** sidebar panel, turn on
**Show in sidebar** in the integration's **Configure** options. It defaults to off,
including after upgrading from 1.6.0. The panel shows active job countdowns,
scheduled actions and retained history through HA's authenticated job WebSocket
API. Once enabled, it stays available when the conversation entry is unloaded.
Hiding it does not change jobs or delivery. The job API and
`.storage/smarthome_jobs.storage` remain compatible.
Version 1.7.0 supplies full timer controls, recurring alarms
and spoken reminders. HA owns their schedules, occurrence outcomes, snooze and
delivery; the panel can create and edit them while Core is offline. Configure an
optional default alert satellite and default snooze duration in HA. Explicit
outputs and the requesting satellite take precedence. Reminders require HA TTS
and announcement support; alarms require native timer presentation. Standard
ESPHome handoff and stop requests do not confirm playback or silence. See
[the job API](jobs.md) and the repository's scheduling guide for supported
recurrence, overdue recovery and protocol guarantees.
New timers use HA-owned native ESPHome
delivery, and remain independent of Core/Gateway after acceptance. Existing timers
retain their stored owner. Finish/cancel active Gateway-owned timers on the old
version before upgrading Core; the HA-only runtime refuses them. Compatible upstream
EchoMuse controllers work with standard delivery; the fork and explicit mapping
are needed only for enhanced acknowledgement/recovery. Unsupported outputs fail
before persistence. See [standard delivery limits](https://github.com/dscrete/smarthome-ha#standard-timer-recovery-limits).

## Capabilities and software verification

The target is exposed-device control and state queries, named timer lifecycle,
recurring alarms and spoken reminders, low-risk one-shot device schedules with
edit/cancel, current/forecast weather and advice,
typed HA scripts with confirmation, clarification and bounded conversation across
wakes. The software baseline passes both repetitions of the eight recorded
conversations: 52/52 turns on 20b low. This earlier language result does not
establish reliability for every
phrasing or physical satellite playback. Missing dependencies or unsupported
outputs report unavailability before creating a job.

The portable software checks prove
expiry, repeated synthetic tone and button dismissal with Core/Gateway stopped,
including HA restart, pre-handoff reconnect and the connected five-minute cutoff.
Standard ESPHome output cannot acknowledge local dismissal or guarantee recovery
of an interrupted handoff. Stock EchoMuse wake dismissal also affects all ringing
Dots on the controller. The optional fork exchange supplies stronger guarantees;
review [the delivery limits](https://github.com/dscrete/smarthome-ha#standard-timer-recovery-limits)
before choosing. Physical playback remains explicitly unverified.

New schedules and rescheduling reject actions classified sensitive by Core's
existing safety policy, even after confirmation. Existing jobs remain inspectable
and cancellable. Immediate sensitive actions and scripts retain exact confirmation.
Routine low-risk actions do not gain a blanket confirmation requirement.

Music expansion, complex event automations, general
undo and long-term memory are deferred. Existing unrelated features are not
removed. The HA agent uses native tools with 20b low; the strict-response experiment
is discontinued.
Ordinary chat uses native speech; device calls retain typed exact-target contracts
and trusted replies. Compound requests may take additional model calls and report
partial completion when a later step cannot finish. No household pipeline changes
are made by selecting or testing the branch in disposable HA.

## Context and confirmation

Core keeps context per integration entry and stable HA satellite/device for 300
seconds of inactivity. A new wake can continue the conversation. History is bounded
to three exchanges and 2,048 serialized UTF-8 bytes. Previous assistant replies above
1,024 bytes become an omission marker; oversized previous user turns are omitted
whole. Current text is never clipped. HA's ChatLog records delivered conversation
and is not forwarded as another history source.

Candidates are distinct from selected targets, observations and permission.
Exposure and live job state are checked again. Text clients without device identity
have isolated conversations.

An explicit approval from the same satellite may approve its exact frozen proposal
after another wake, within 30 seconds. A wake alone, another satellite, an expired
proposal, replacement request or stale target/revision cannot approve it. Refusal
discards the proposal. Conversation history never grants authority.

## Operation and failures

Pairing uses one explicit approval in HA and Core's existing authenticated HA
connection. Unapproved offers cannot authorize turns. Reconfiguration can rotate
the automatic credential; removal revokes it when Core is reachable. For setup,
offline recovery and manual compatibility, see
[pairing and connection security](https://github.com/dscrete/smarthome-ha#pairing-and-connection-security).
Core persists only its hash in `data/ha-agent/pairing.json`; HA holds the raw
credential privately. Keep that state separate from the request journal below.

The bearer-authenticated protocol is `GET /ha/v1/info` and `POST /ha/v1/turn`,
version 1. Only final utterances are submitted. Request IDs must never be reused
for new work; replay protection covers **24 hours** from first acceptance.
Core records each request before dispatch in
`data/ha-agent/requests.db`, bind-mounted inside the project. Interrupted requests
remain unknown after Core restart and never replay automatically.

The journal prunes on startup, new requests and hourly while running. It retains
delivered replies/action receipts, omits raw diagnostic observations, and limits
stored responses to 64 KiB. An oversized response retains its request ID with an
unknown replay result, so it cannot be executed again to reconstruct a reply.
At 10,000 protected requests, new work fails unavailable before dispatch; recent
request IDs are never evicted to make room. Old journal rows without timestamps
gain a full 24-hour window on upgrade because their original acceptance times
were not recorded. Existing timestamped rows keep their original acceptance time;
rows older than 24 hours are removed on startup. Do not reuse an expired ID:
after the window it is no longer known to the server.
The replay window is advertised by `GET /ha/v1/info`. These are fixed protocol
limits, independent of the five-minute conversational memory.

There are no timeout retries or fallback conversation engines. A timeout can mean
execution already happened; check the actual device/job before repeating it.
Missing dependencies produce explicit unavailable/unknown feedback. Native tools
currently require the configured Groq primary; this route does not use the configured
fallback. GPT-OSS 20b remains the default.

The retired `/satellites/ws` Core audio endpoint and standalone Gateway/client are
removed. Core requires no speech services; HA handles voice. The optional
`profile_id` on authenticated text turns comes from agent configuration, never
detected wake metadata. An unknown explicit persona fails before any model call
or action. Omitted IDs use Core's configured fallback. Automatic per-wake persona
switching is deferred. Previously protected request IDs keep their replay window.

Unloading stops the agent; HA may retain an unavailable restored entity until it
is loaded again. Unloading or removal does not cancel HA-owned jobs or delete
Core's request journal. Keep credentials and the journal out of Git. Remove
pipeline references before removing their agent.

The adapter follows the supported
[HA conversation entity contract](https://developers.home-assistant.io/docs/core/entity/conversation/).
