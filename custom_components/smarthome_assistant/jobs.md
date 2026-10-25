# SmartHome assistant job API

Jobs are part of the single `smarthome_assistant` integration. No separate jobs
integration or YAML block is required. The integration
persists job metadata using HA's versioned Store and exposes commands through the
authenticated Home Assistant WebSocket API. Scheduled home-action payloads are
executed by trusted Core; HA delivers timers, alarms and spoken reminders. The `smarthome_jobs` API and
storage names remain for compatibility.

All job commands, including listing and exposure checks, require an authenticated
HA administrator. Configure Core's `HA_TOKEN` with that role; ordinary authenticated
users cannot create jobs that Core would later execute with its own credentials.
The boundary uses HA's supported `websocket_api.require_admin` decorator.
Stored payloads and their `requires_confirmation` flags never prove user approval:
Core reloads the current action contract and rejects sensitive scheduled actions
at dispatch, even if a job was submitted outside the normal scheduling path.

Commands are `smarthome_jobs/create`, `update`, `cancel`, `list`, `claim`,
`complete`, `timer_dismiss`, `operate`, `timer_cancel_preview`, `timer_cancel_batch`,
and `ack_notice`. Single-job mutations return `{job: <full
record>}`; list returns `{jobs: [<full record>, ...]}`. Create takes
`{request_id,job:{kind,due_at,payload,source_satellite?,name?}}`. Update takes
`{request_id,job_id,expected_revision,changes}`. Cancel, claim, timer_dismiss,
and ack_notice take `{request_id,job_id,expected_revision}`. Complete additionally
takes `outcome`, which is `succeeded|failed|unknown|missed` for scheduled actions
and `undelivered` for timers. List filters are optional `statuses`, `kinds`, and
exact `source_satellite`; all supplied filters apply together.
Timer list records include `allowed_operations`. `operate` takes
`{request_id,job_id,expected_revision,operation,arguments?}`. It supports
`pause`, `resume`, `adjust_remaining` (`{delta_seconds}`), `set_remaining`
(`{seconds}`), `rename` (`{name}`), and `restart`. Paused timers persist their
frozen positive remaining seconds across HA restarts. Restart creates a linked
countdown using the original duration and output identity; restarting a live
timer atomically marks its prior record `cancelled` with outcome `restarted`.
Timing and name edits stop once a timer is due or sounding. Revision conflicts
and request-id replay protection apply to every operation.

## Bulk timer cancellation and delivery facts

`timer_cancel_preview` is an administrator-only read. It accepts optional
`source_satellite` (the authenticated requesting HA identity) and `context_source`
(Core's opaque conversation-source reference). It returns
`{selection:{source_satellite,context_source,jobs:[{id,revision,name,duration}]}}`.
HA resolves the physical source and selects active timers. Timers carrying a saved
`payload.context_source` require that exact context; timers without one use their
persisted output identity. A disconnected output does not require fallback to a
different speaker to cancel its accepted jobs. A preview changes no state.

After the user confirms the exact proposal, send the unchanged selection to
`timer_cancel_batch` with a `request_id`. HA rechecks each ID, revision, active
state and source membership. Only the saved IDs are eligible: a timer created
after preview is excluded. The batch saves valid changes and its request receipt
before updating delivery. Invalid selection structure is rejected before writes;
an empty new selection returns `no_match` without a write. A selection contains
at most 256 timers, and preview rejects a larger set before confirmation.

The response contains `status: completed|partial|failed|no_match`, per-timer
`receipts`, and aggregate `delivery_facts`. Changed/missing/foreign members are
reported individually while unchanged valid members can succeed. Replaying a
saved request ID returns the original result, including after HA restart. Core
retains the short-lived proposal and confirmation but does not list, filter,
revalidate or cancel individual members. A lost response is unknown to the client
and must not trigger automatic replay.

Timer/alarm list, selection and stop-operation results expose
`delivery_facts.stop_confirmed`. It is true only for validated stop evidence from
the accepted presentation. A state transition, missing delivery receipt, standard
handoff or stop submission cannot prove silence. Bulk results additionally expose
`delivery_facts.stop_unconfirmed` when a relevant stop lacks acknowledgement, so
clients need not derive that distinction from per-member status or receipts.
Snooze results report stop facts for the prior occurrence, separately from the
new countdown. Clients render HA's facts and treat missing facts as unconfirmed.

These commands and facts require SmartHome Assistant 1.7.0 or newer. Update the
integration alongside Core; older single-job clients remain supported. There is
no fallback that moves bulk selection or receipt interpretation back into Core.

## HA panel

The integration offers an administrator-only SmartHome sidebar panel, enabled
through **Configure → Show in sidebar**. This HA integration preference defaults
to false, also for existing entries without an explicit choice. Disabling the
panel removes its route without changing jobs, delivery or job API access. An
enabled panel stays available across ordinary conversation entry unloads; removing
the config entry removes the panel. It shows active countdowns, scheduled action
summaries and retained history. The panel subscribes with `smarthome_jobs/subscribe`; HA sends an empty
invalidation event after each durable saved change, and the panel refreshes via
`list`. HA removes the listener with the WebSocket connection subscription.
Cancel and dismissal use the existing request ID and expected revision contract.
Pending/paused/due timers can be cancelled, and timers in the backend's
`sounding` state expose dismissal. A standard ESPHome completed `handed_off`
timer is history only: HA cannot confirm that its alert is sounding or receive a
local dismissal, so the panel states that delivery limit and offers no dismissal.

The panel creates and edits timers, alarms and reminders through the same API.
Each listed record includes `allowed_operations`; unsupported controls are not
invented in the browser. The panel displays standard handoff/stop uncertainty
separately from acknowledged enhanced delivery.

## Alarm/reminder definitions and semantic scheduling

Create also accepts `kind: alarm|reminder`. A reminder's payload is
`{message: <saved text>}`; alarm payload is empty. `schedule` replaces `due_at`
for typed timing requests. Timer relative creation uses
`{kind: relative, seconds: <positive integer>}` and HA's current clock.
The same relative form creates one-shot alarms/reminders. Calendar forms are:

| Schedule kind | Fields |
| --- | --- |
| `once` | `time: HH:MM[:SS]`, optional `date: YYYY-MM-DD` (omission means next future clock time) |
| `daily` | `time` |
| `weekly` | `time`, unique `weekdays: [0..6]`, Monday = 0 |
| `monthly` | `time`, `day: 1..31` |
| `interval` | positive integer `every`, `unit: minutes|hours|days|weeks`, optional local `anchor: YYYY-MM-DDTHH:MM[:SS]` |

Calendar forms accept an optional IANA `timezone`, captured from HA by default.
Unknown fields and conflicting timing inputs are rejected. See the scheduling
guide for DST, monthly-date and overdue behavior. Legacy absolute `due_at`
creation remains supported for existing timers and device-action schedules.

Alarm/reminder creation returns `{job: <first occurrence>, schedule: <definition>}`.
`list` with `include_schedules: true` adds `schedules` alongside `jobs`. A definition
and its occurrences have separate IDs and revisions. Retained occurrences describe
delivery history; disabling/editing a definition does not rewrite those outcomes.

`operate` accepts exactly one target: `job_id` or `schedule_id`, plus
`request_id`, `expected_revision`, `operation` and optional `arguments`.
Definition operations are `enable`, `disable`, `edit`, `cancel`; `edit` supplies
the changed name, schedule, reminder payload or `source_satellite`. At creation,
`source_satellite` is the explicit output; optional `context_satellite` is the
authenticated requesting satellite. Only when both are absent may HA use its
configured default. Alarm occurrence operations are
`dismiss` and `snooze`; snooze accepts optional `{seconds: N}` or uses HA's
configured default. A snooze does not change the regular schedule.

Administrator `outputs` returns registered Assist satellites with names, areas,
availability and timer/announcement support, plus HA's timezone, default output
and snooze duration. Future alert creation can bind a supported offline satellite;
new timers require connected verification. Accepted output identities and protocols
cannot silently change.

Administrator `resolve` accepts a `selection` object containing `kinds`, optional `operation`, `source_satellite`,
`recent_id`, `target_type: job|schedule`, and a typed `selector`:
`{kind: name, name}`, `{kind: duration, duration_seconds}`, `{kind: recent}`,
`{kind: automatic}`, or `{kind: id, job_id}` (use `schedule_id` for definition
targets). `area_id` may constrain a selector.
Results distinguish selected, no-match and ambiguous candidates. Clients retain
exact revisions for subsequent mutations; a selection read never authorizes a write.

Due jobs emit `smarthome_jobs_due` with `{job_id,kind,revision,due_at,status}`.
Every mutation uses a client request ID so retrying after a lost response returns
the original result. Expected revisions reject stale writes. Scheduled-action
payloads are opaque Core data; HA does not interpret or dispatch them. Restart
reconciliation skips actions due while HA was offline, marks an action claimed
before restart as unknown, and retains sounding timers until dismissal or the
five-minute delivery cutoff. Terminal problem notices are stored on the job and
shown as HA persistent notifications.

## Timer delivery protocols

New connected ESPHome Assist satellites with native timer support automatically
use `delivery.owner: ha`, `delivery.protocol: assist_native`, and a persisted
HA device identity. Explicit enhanced output mappings keep the acknowledged
EchoMuse exchange instead; an unavailable mapped exchange never silently falls
back to native events. Existing timers retain their accepted delivery system.

For native events, HA saves `finish_attempted_at` before dispatch. Successful
submission becomes `status: completed`, `outcome: handed_off`, with
`transport_sent_at`; it deliberately has no `delivered_at` acknowledgement.
An interrupted or failed reserved handoff becomes `unknown` /
`native_delivery_unknown`, generates a notice, and is never replayed. A name
announcement is bounded and may be uncertain without preventing the tone.
Standard local dismissal is not reported to HA. HA attempts CANCELLED at the
original due-plus-five-minute cutoff when connected. The enhanced exchange keeps
its existing acknowledged presentation and dismissal semantics.

The storage document is version 4; HA Store's outer envelope and the
`smarthome_jobs.storage` key remain unchanged. Versions 1, 2 and 3 migrate without
changing existing jobs' owners, authority or request receipts. Older integrations
cannot read v4: take an HA backup
before upgrading and restore it for a deliberate downgrade. Never delete the
store to resolve a version mismatch. The administrator job API stays compatible;
Core's legacy claim/completion and exchange receipts cannot claim a native job.
