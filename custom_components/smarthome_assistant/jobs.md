# SmartHome assistant job API

Jobs are part of the single `smarthome_assistant` integration. No separate jobs
integration or YAML block is required. The integration
persists job metadata using HA's versioned Store and exposes commands through the
authenticated Home Assistant WebSocket API. Scheduled home-action payloads are
executed by trusted Core; HA delivers native timers. The `smarthome_jobs` API and
storage names remain for compatibility.

All job commands, including listing and exposure checks, require an authenticated
HA administrator. Configure Core's `HA_TOKEN` with that role; ordinary authenticated
users cannot create jobs that Core would later execute with its own credentials.
The boundary uses HA's supported `websocket_api.require_admin` decorator.
Stored payloads and their `requires_confirmation` flags never prove user approval:
Core reloads the current action contract and rejects sensitive scheduled actions
at dispatch, even if a job was submitted outside the normal scheduling path.

Commands are `smarthome_jobs/create`, `update`, `cancel`, `list`, `claim`,
`complete`, `timer_dismiss`, and `ack_notice`. Mutations return `{job: <full
record>}`; list returns `{jobs: [<full record>, ...]}`. Create takes
`{request_id,job:{kind,due_at,payload,source_satellite?,name?}}`. Update takes
`{request_id,job_id,expected_revision,changes}`. Cancel, claim, timer_dismiss,
and ack_notice take `{request_id,job_id,expected_revision}`. Complete additionally
takes `outcome`, which is `succeeded|failed|unknown|missed` for scheduled actions
and `undelivered` for timers. List filters are optional `statuses`, `kinds`, and
exact `source_satellite`; all supplied filters apply together.

Due jobs emit `smarthome_jobs_due` with `{job_id,kind,revision,due_at,status}`.
Every mutation uses a client request ID so retrying after a lost response returns
the original result. Expected revisions reject stale writes. Scheduled-action
payloads are opaque Core data; HA does not interpret or dispatch them. Restart
reconciliation skips actions due while HA was offline, marks an action claimed
before restart as unknown, and retains sounding timers until dismissal or the
five-minute delivery cutoff. Terminal problem notices are stored on the job and
shown as HA persistent notifications.
