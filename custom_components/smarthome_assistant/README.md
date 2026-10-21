# SmartHome Assistant for HA Assist

This project-owned integration is opt-in. It exposes a conversation entity;
Home Assistant runs STT, ordinary TTS, and follow-up listening. Core owns final-text
interpretation, validation, exact-target execution, confirmation and trusted replies.
The integration does not install a model or change other pipelines.

## Install and select

1. Install [SmartHome Assistant through HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=dscrete&repository=smarthome-ha&category=integration) and restart HA.
   Released updates use HACS-provided HA update entities. Developer installs can
   still copy this one folder into `config/custom_components/smarthome_assistant`.
2. In SmartHome Settings → Home Assistant, set **HA conversation agent
   credential** (`CORE_HA_AGENT_TOKEN`) to a dedicated random secret and restart
   Core. This is separate from the Gateway alert secret. Empty disables the route.
3. In HA → Settings → Devices & services → Add integration, select **SmartHome
   Assistant**. Enter Core's reachable HTTP(S) URL and that credential. Use HTTPS
   outside a trusted local network. Never put credentials in the URL.
4. Create a separate HA voice assistant/pipeline selecting the SmartHome
   conversation entity and your existing STT/TTS. **Disable “Prefer handling
   commands locally”**. Otherwise native HA intents, including timers, can bypass
   Core's backend, validation and confirmation.
5. Assign only the intended test satellite to that pipeline. In EchoMuse,
   choose its **Home Assistant voice backend** when using this pipeline.
   Physical EchoMuse playback remains unverified until an isolated hardware test.
6. Standard timer-capable ESPHome Assist satellites need no output mapping.
   For optional enhanced EchoMuse recovery, enter its exchange mapping in
   Configure, following [installation and migration](https://github.com/dscrete/smarthome-ha).
   No separate jobs folder or YAML block is needed. The Gateway-binding field
   remains for compatibility; new timers do not use it.

The `smarthome_jobs` backend is included in this integration for timers and
schedules. Its API and `.storage/smarthome_jobs.storage` remain compatible. Existing Scheduler
Component jobs are not migrated or deleted. New timers use HA-owned native ESPHome
delivery, and remain independent of Core/Gateway after acceptance. Existing timers
retain their legacy owner until finished or cancelled. Compatible upstream
EchoMuse controllers work with standard delivery; the fork and explicit mapping
are needed only for enhanced acknowledgement/recovery. Unsupported outputs fail
before persistence. See [standard delivery limits](https://github.com/dscrete/smarthome-ha#standard-timer-recovery-limits).

## Current merge scope

The target is exposed-device control and state queries, named timer lifecycle,
low-risk one-shot schedules with edit/cancel, current/forecast weather and advice,
typed HA scripts with confirmation, clarification and bounded conversation across
wakes. The software baseline passes both repetitions of the eight recorded
conversations: 52/52 turns on 20b low. This earlier language result does not
establish reliability for every
phrasing or physical satellite playback. Missing dependencies or unsupported
outputs report unavailability before creating a job.

**The development PR remains in draft.** The portable software checks prove
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

Music expansion, reminders/recurring alarms, complex event automations, general
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

Unloading stops the agent; HA may retain an unavailable restored entity until it
is loaded again. Unloading or removal does not cancel HA-owned jobs or delete
Core's request journal. Keep credentials and the journal out of Git. Remove
pipeline references before removing their agent.

The adapter follows the supported
[HA conversation entity contract](https://developers.home-assistant.io/docs/core/entity/conversation/).
