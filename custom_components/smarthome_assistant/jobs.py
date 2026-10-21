"""Persistent state machine for SmartHome jobs stored by Home Assistant.

The HA-authenticated WebSocket commands are smarthome_jobs/create, update,
cancel, list, claim, complete, and timer_dismiss. Mutations use request_id for
idempotency. Job inputs contain kind, due_at (RFC3339), payload, and optional
source_satellite/name. HA stores jobs and projects native timer presentation;
scheduled home-action payloads are executed only by trusted Core. Due events use smarthome_jobs_due with
job_id, kind, revision, due_at, and status.
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

DOMAIN = "smarthome_jobs"
EVENT_JOB_DUE = "smarthome_jobs_due"
STORE_VERSION = 3
NATIVE_PROTOCOL = "assist_native"
# The HA Store envelope remains compatible; the versioned job document migrates
# below. No existing job changes delivery owner during an upgrade.
STORAGE_VERSION = 1
ACTION_GRACE = timedelta(seconds=30)
CLAIM_TIMEOUT = timedelta(seconds=60)
TIMER_GRACE = timedelta(minutes=5)
JOB_RETENTION = timedelta(days=30)
REQUEST_RETENTION = timedelta(days=7)
RECONCILE_TERMINAL_GRACE = timedelta(minutes=10)
TERMINAL_STATES = {"completed", "failed", "unknown", "missed", "expired", "dismissed", "cancelled"}


class JobError(Exception):
    """A safe command error returned to the authenticated client."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise JobError("invalid_format", "due_at must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JobError("invalid_format", "due_at must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JobError("invalid_format", "due_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_payload(kind: str, payload: Any) -> dict[str, Any]:
    """Validate the Core wire shape without interpreting or executing it."""
    if not isinstance(payload, dict):
        raise JobError("invalid_format", "payload must be an object")
    if kind == "timer":
        seconds = payload.get("duration_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            raise JobError("invalid_format", "timer payload requires positive integer duration_seconds")
    else:
        if not isinstance(payload.get("capability_id"), str) or not payload["capability_id"]:
            raise JobError("invalid_format", "scheduled action requires capability_id")
        if not isinstance(payload.get("action_id"), str) or not payload["action_id"]:
            raise JobError("invalid_format", "scheduled action requires action_id")
        if not isinstance(payload.get("target"), dict) or not isinstance(payload.get("fields"), dict):
            raise JobError("invalid_format", "scheduled action requires object target and fields")
        if not isinstance(payload.get("requires_confirmation"), bool):
            raise JobError("invalid_format", "scheduled action requires boolean requires_confirmation")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise JobError("invalid_format", "payload must contain JSON values") from exc
    return copy.deepcopy(payload)


def validate_job_input(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise JobError("invalid_format", "job must be an object")
    kind = raw.get("kind")
    if kind not in {"scheduled_action", "timer"}:
        raise JobError("invalid_format", "kind must be scheduled_action or timer")
    payload = _validate_payload(kind, raw.get("payload", {}))
    name, source = raw.get("name"), raw.get("source_satellite")
    if name is not None and (not isinstance(name, str) or len(name) > 160):
        raise JobError("invalid_format", "name must be a string of at most 160 characters")
    if source is not None and (not isinstance(source, str) or len(source) > 160):
        raise JobError("invalid_format", "source_satellite must be a string of at most 160 characters")
    return {
        "kind": kind, "due_at": iso_utc(parse_utc(raw.get("due_at"))),
        "payload": payload, "source_satellite": source, "name": name,
    }


class JobsManager:
    """Versioned persistent jobs with explicit, non-replaying transitions."""

    def __init__(self, hass: Any, store: Any, *, now: Callable[[], datetime],
                 schedule_at: Callable[[Callable[[datetime], None], datetime], Callable[[], None]],
                 notify: Callable[[dict[str, Any], str], None], timer_output=None,
                 clear_notice=None):
        self.hass, self.store, self.now = hass, store, now
        self.schedule_at, self.notify = schedule_at, notify
        self.jobs: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self._scheduled: dict[str, Callable[[], None]] = {}
        self._lock = asyncio.Lock()
        self.timer_output = timer_output
        self.clear_notice = clear_notice or (lambda job: None)
        self.on_change = lambda: None
        self.authority_id = str(uuid.uuid4())
        self._sequence_base = 0
        self._sequence_counter = 0
        self._committed = {"jobs": {}, "requests": {}}

    async def async_load(self) -> None:
        data = await self.store.async_load()
        if data:
            if data.get("version") not in {1, 2, STORE_VERSION}:
                raise RuntimeError(f"Unsupported {DOMAIN} storage version")
            self.jobs, self.requests = data.get("jobs", {}), data.get("requests", {})
            self.authority_id = data.get("authority_id") or self.authority_id
            self._sequence_base = data.get("sequence_base", 0)
        self._committed = copy.deepcopy({"jobs": self.jobs, "requests": self.requests})
        for job in self.jobs.values():
            if job["kind"] == "timer":
                job.setdefault("delivery", {"owner": "core_gateway"})
        self._sequence_base += 2 ** 32
        # Reserve an entire sequence range durably before any network delivery.
        await self._save()
        await self._reconcile_startup()
        if self._prune():
            await self._save()

    async def _save(self) -> None:
        self._prune()
        document = copy.deepcopy({"version": STORE_VERSION, "jobs": self.jobs, "requests": self.requests,
                                 "authority_id": self.authority_id, "sequence_base": self._sequence_base})
        try:
            await self.store.async_save(document)
        except BaseException:
            # The worker must never project a mutation that HA did not save.
            self.jobs = copy.deepcopy(self._committed["jobs"])
            self.requests = copy.deepcopy(self._committed["requests"])
            raise
        self._committed = document
        self.on_change()

    def delivery_sequence(self) -> int:
        self._sequence_counter += 1
        if self._sequence_counter >= 2 ** 32:
            raise JobError("unavailable", "delivery sequence range exhausted; restart HA")
        return self._sequence_base + self._sequence_counter

    def _prune(self) -> bool:
        now = self.now().astimezone(timezone.utc)
        changed = False
        for job_id, job in list(self.jobs.items()):
            if job["status"] not in TERMINAL_STATES:
                continue
            if not job.get("finished_at"):
                job["finished_at"] = iso_utc(now)
                changed = True
            if parse_utc(job["finished_at"]) < now - JOB_RETENTION:
                self.jobs.pop(job_id)
                changed = True
        for request_id, result in list(self.requests.items()):
            if not result.get("at"):
                result["at"] = iso_utc(now)
                changed = True
            if parse_utc(result["at"]) < now - REQUEST_RETENTION:
                self.requests.pop(request_id)
                changed = True
        return changed

    def _job(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise JobError("not_found", "job does not exist")
        return self.jobs[job_id]

    @staticmethod
    def _revision(job: dict[str, Any], expected: Any) -> None:
        if expected != job["revision"]:
            raise JobError("revision_conflict", "job revision has changed")

    def _replay(self, request_id: str) -> dict[str, Any] | None:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 160:
            raise JobError("invalid_format", "request_id must be a non-empty string")
        result = self.requests.get(request_id)
        if result is not None and result.get("at") and (
            parse_utc(result["at"]) < self.now().astimezone(timezone.utc) - REQUEST_RETENTION
        ):
            return None
        return copy.deepcopy(result) if result is not None else None

    def _remember(self, request_id: str, result: dict[str, Any]) -> None:
        self.requests[request_id] = {**copy.deepcopy(result), "at": iso_utc(self.now())}

    def _emit_due(self, job: dict[str, Any]) -> None:
        self.hass.bus.async_fire(EVENT_JOB_DUE, {
            "job_id": job["id"], "kind": job["kind"], "revision": job["revision"],
            "due_at": job["due_at"], "status": job["status"],
        })

    def _cancel_schedule(self, job_id: str) -> None:
        cancel = self._scheduled.pop(job_id, None)
        if cancel:
            cancel()

    def _schedule_job(self, job: dict[str, Any]) -> None:
        self._cancel_schedule(job["id"])
        due = parse_utc(job["due_at"])
        if job["kind"] == "scheduled_action" and job["status"] == "pending":
            when = max(due, self.now())
            self._scheduled[job["id"]] = self.schedule_at(
                lambda _now, jid=job["id"]: self.hass.async_create_task(self._action_due(jid)), when)
        elif job["kind"] == "scheduled_action" and job["status"] == "claimed":
            claimed = parse_utc(job["claimed_at"])
            self._scheduled[job["id"]] = self.schedule_at(
                lambda _now, jid=job["id"]: self.hass.async_create_task(self._abandoned_claim(jid)),
                max(claimed + CLAIM_TIMEOUT + timedelta(seconds=1), self.now()))
        elif job["kind"] == "timer" and job["status"] == "pending":
            self._scheduled[job["id"]] = self.schedule_at(
                lambda _now, jid=job["id"]: self.hass.async_create_task(self._timer_due(jid)), max(due, self.now()))
        elif job["kind"] == "timer" and job["status"] in {"due", "sounding"}:
            self._scheduled[job["id"]] = self.schedule_at(
                lambda _now, jid=job["id"]: self.hass.async_create_task(self._timer_expired(jid)),
                max(due + TIMER_GRACE + timedelta(seconds=1), self.now()))

    async def _reconcile_startup(self) -> None:
        now = self.now().astimezone(timezone.utc)
        changed, due_events, notices = False, [], []
        for job in self.jobs.values():
            due, status = parse_utc(job["due_at"]), job["status"]
            if (job.get("delivery", {}).get("protocol") == NATIVE_PROTOCOL
                    and status == "due" and job["delivery"].get("finish_attempted_at")):
                # Standard satellites cannot durably acknowledge dismissal.
                # A reserved finish may have reached the output: never replay it.
                job.update(status="unknown", outcome="native_delivery_unknown", notice_spoken=False,
                           revision=job["revision"] + 1)
                changed = True
                notices.append((job, "native_unknown"))
                continue
            if job["kind"] == "scheduled_action":
                if status == "claimed":
                    job.update(status="unknown", outcome="unknown", notice_spoken=False, revision=job["revision"] + 1)
                    changed = True
                    notices.append((job, "unknown"))
                elif status == "pending" and due <= now:
                    job.update(status="missed", outcome="missed", notice_spoken=False, revision=job["revision"] + 1)
                    changed = True
                    notices.append((job, "missed"))
                elif status == "pending":
                    self._schedule_job(job)
            elif status in {"pending", "due", "sounding"}:
                if now - due > TIMER_GRACE:
                    if job.get("delivered_at"):
                        job.update(status="expired", outcome="auto_stopped", revision=job["revision"] + 1)
                    else:
                        job.update(status="missed", outcome="undelivered", notice_spoken=False,
                                   revision=job["revision"] + 1)
                        notices.append((job, "undelivered"))
                    changed = True
                elif due <= now:
                    if status == "pending":
                        job.update(status="due", revision=job["revision"] + 1)
                        changed = True
                    self._schedule_job(job)
                    due_events.append(job)
                else:
                    self._schedule_job(job)
        if changed:
            await self._save()
        for job, reason in notices:
            self.notify(job, reason)
        for job in due_events:
            self._emit_due(job)

    async def async_create(self, request_id: str, raw: Any) -> dict[str, Any]:
        # Availability checks may use HA's service connection. Never hold the
        # state lock across that network work or a receipt exchange can deadlock.
        delivery = None
        if isinstance(raw, dict) and raw.get("kind") == "timer" and self.timer_output:
            async with self._lock:
                replay = self._replay(request_id)
                if replay is not None:
                    return replay["job"]
            output = await self.timer_output(raw.get("source_satellite"))
            delivery = {"owner": "ha", "output": output["output"], "token": str(uuid.uuid4()), "receipts": {}}
            if output.get("delivery_protocol") == NATIVE_PROTOCOL:
                delivery.update(protocol=NATIVE_PROTOCOL, device_id=output["device_id"])
            if output.get("mac_address"):
                delivery.update(mac_address=output["mac_address"].replace(":", "").replace("-", "").lower(),
                                controller_device_id=output.get("controller_device_id"))
        async with self._lock:
            replay = self._replay(request_id)
            if replay is not None:
                return replay["job"]
            fields, now = validate_job_input(raw), self.now().astimezone(timezone.utc)
            job = {"id": str(uuid.uuid4()), **fields, "status": "pending", "revision": 1,
                   "created_at": iso_utc(now)}
            if job["kind"] == "timer":
                job["delivery"] = delivery or {"owner": "core_gateway"}
                if delivery:
                    job["source_satellite"] = delivery["output"]
            due, notification, emit = parse_utc(job["due_at"]), None, False
            if due <= now:
                if job["kind"] == "scheduled_action" and now - due > ACTION_GRACE:
                    job.update(status="missed", outcome="missed", notice_spoken=False, revision=2); notification = "missed"
                elif job["kind"] == "scheduled_action":
                    job.update(notice_spoken=False); emit = True
                elif now - due <= TIMER_GRACE:
                    job.update(status="due", revision=2); emit = True
                else:
                    job.update(status="missed", outcome="undelivered", notice_spoken=False, revision=2); notification = "undelivered"
            self.jobs[job["id"]] = job
            result = copy.deepcopy(job)
            self._remember(request_id, {"job": result})
            await self._save()
            if notification:
                self.notify(job, notification)
            elif emit:
                self._emit_due(job)
                if job["kind"] == "scheduled_action": self._schedule_action_grace(job)
                else: self._schedule_job(job)
            else:
                self._schedule_job(job)
            return result

    async def async_update(self, request_id: str, job_id: str, expected_revision: int, changes: Any) -> dict[str, Any]:
        async with self._lock:
            replay = self._replay(request_id)
            if replay is not None: return replay["job"]
            job = self._job(job_id); self._revision(job, expected_revision)
            if job["status"] != "pending": raise JobError("invalid_state", "only pending jobs can be updated")
            allowed = {"due_at", "payload", "source_satellite", "name"}
            if not isinstance(changes, dict) or not changes or set(changes) - allowed:
                raise JobError("invalid_format", "changes must contain supported job fields")
            updated = copy.deepcopy(job)
            if "due_at" in changes: updated["due_at"] = iso_utc(parse_utc(changes["due_at"]))
            if "payload" in changes:
                updated["payload"] = _validate_payload(updated["kind"], changes["payload"])
            if job.get("delivery", {}).get("owner") == "ha":
                if changes.get("source_satellite", job["source_satellite"]) != job["source_satellite"]:
                    raise JobError("invalid_format", "an accepted timer cannot change its output")
                if "due_at" in changes:
                    updated["delivery"]["token"] = str(uuid.uuid4())
                    updated["delivery"]["receipts"] = {}
            for key in ("source_satellite", "name"):
                if key in changes and changes[key] is not None and (not isinstance(changes[key], str) or len(changes[key]) > 160):
                    raise JobError("invalid_format", f"{key} must be a string of at most 160 characters")
                if key in changes: updated[key] = changes[key]
            updated["revision"] += 1
            now, due = self.now().astimezone(timezone.utc), parse_utc(updated["due_at"])
            notification, emit = None, False
            if due <= now:
                if updated["kind"] == "scheduled_action" and now - due > ACTION_GRACE:
                    updated.update(status="missed", outcome="missed", notice_spoken=False, revision=updated["revision"] + 1); notification = "missed"
                elif updated["kind"] == "scheduled_action":
                    updated["notice_spoken"] = False; emit = True
                elif now - due <= TIMER_GRACE:
                    updated.update(status="due", revision=updated["revision"] + 1); emit = True
                else:
                    updated.update(status="missed", outcome="undelivered", notice_spoken=False, revision=updated["revision"] + 1); notification = "undelivered"
            self.jobs[job_id] = updated
            result = copy.deepcopy(updated); self._remember(request_id, {"job": result})
            await self._save()
            if notification: self.notify(updated, notification)
            elif emit:
                self._emit_due(updated)
                if updated["kind"] == "scheduled_action": self._schedule_action_grace(updated)
                else: self._schedule_job(updated)
            else: self._schedule_job(updated)
            return result

    async def async_cancel(self, request_id: str, job_id: str, expected_revision: int) -> dict[str, Any]:
        async with self._lock:
            replay = self._replay(request_id)
            if replay is not None: return replay["job"]
            job = self._job(job_id); self._revision(job, expected_revision)
            if job["status"] not in {"pending", "due"}: raise JobError("invalid_state", "only pending or due jobs can be cancelled")
            job.update(status="cancelled", revision=job["revision"] + 1)
            result = copy.deepcopy(job); self._remember(request_id, {"job": result})
            await self._save(); self._cancel_schedule(job_id)
            return result

    async def async_list(self, *, statuses: list[str] | None = None, kinds: list[str] | None = None,
                         source_satellite: str | None = None,
                         for_reconcile: bool = False) -> list[dict[str, Any]]:
        async with self._lock:
            now = self.now().astimezone(timezone.utc)
            return [copy.deepcopy(job) for job in self.jobs.values()
                    if (statuses is None or job["status"] in statuses)
                    and (kinds is None or job["kind"] in kinds)
                    and (source_satellite is None or job["source_satellite"] == source_satellite)
                    and (not for_reconcile or job.get("delivery", {}).get("owner") != "ha"
                         or (job["status"] in {"missed", "failed", "unknown"} and job.get("notice_spoken") is False))
                    and (not for_reconcile or job["status"] not in TERMINAL_STATES
                         or (job["status"] in {"missed", "failed", "unknown"}
                             and job.get("notice_spoken") is False)
                         or (job["kind"] == "timer" and job.get("finished_at")
                             and parse_utc(job["finished_at"]) >= now - RECONCILE_TERMINAL_GRACE))]

    async def async_validate_output_bindings(self, bindings) -> None:
        """Do not detach an active timer's configured output during UI edits."""
        old = getattr(self, "delivery_worker", None)
        if old is None:
            return
        jobs = await self.async_list(kinds=["timer"], statuses=["pending", "due", "sounding"])
        for job in jobs:
            delivery = job.get("delivery", {})
            output = delivery.get("output")
            if (delivery.get("owner") == "ha" and output in old.bindings
                    and old.bindings[output] != bindings.get(output)):
                raise ValueError("Finish or cancel active timers before changing their output binding")

    async def async_native_begin(self, job_id: str, expected_revision: int) -> dict | None:
        """Save a one-shot native finish reservation before any output work."""
        async with self._lock:
            job = self._job(job_id)
            if (job["revision"] != expected_revision or job["status"] != "due"
                    or job.get("delivery", {}).get("protocol") != NATIVE_PROTOCOL
                    or job["delivery"].get("finish_attempted_at")
                    or not parse_utc(job["due_at"]) <= self.now() <= parse_utc(job["due_at"]) + TIMER_GRACE):
                return None
            job["delivery"]["finish_attempted_at"] = iso_utc(self.now())
            job["revision"] += 1
            await self._save()
            return copy.deepcopy(job)

    async def async_native_finish(self, job_id: str, expected_revision: int, send, *, name_status: str) -> bool:
        """Send once under the state guard; transport submission is not playback."""
        async with self._lock:
            job = self._job(job_id)
            if (job["revision"] != expected_revision or job["status"] != "due"
                    or job.get("delivery", {}).get("protocol") != NATIVE_PROTOCOL
                    or not job["delivery"].get("finish_attempted_at")
                    or self.now() > parse_utc(job["due_at"]) + TIMER_GRACE):
                return False
            try:
                # The HA timer handler only queues a standard protocol message;
                # it must be synchronous, with no network await under this lock.
                if self.now() < parse_utc(job["due_at"]):
                    raise JobError("not_due", "HA's clock moved before the reserved timer due time")
                send(copy.deepcopy(job))
            except Exception:
                job.update(status="unknown", outcome="native_delivery_unknown", notice_spoken=False)
                reason = "native_unknown"
            else:
                job.update(status="completed", outcome="handed_off")
                job["delivery"]["transport_sent_at"] = iso_utc(self.now())
                reason = None
            job["delivery"]["name_status"] = name_status
            job["revision"] += 1
            await self._save()
            self._cancel_schedule(job_id)
            if reason:
                self.notify(job, reason)
            return reason is None

    async def async_claim(self, request_id: str, job_id: str, expected_revision: int) -> dict[str, Any]:
        async with self._lock:
            replay = self._replay(request_id)
            if replay is not None: return replay["job"]
            job = self._job(job_id); self._revision(job, expected_revision)
            if job.get("delivery", {}).get("owner") == "ha":
                raise JobError("delivery_owned_by_ha", "HA delivers this timer; Core cannot claim it")
            now, due = self.now().astimezone(timezone.utc), parse_utc(job["due_at"])
            notification = None
            if job["kind"] == "scheduled_action":
                if job["status"] != "pending": raise JobError("invalid_state", "scheduled action is not claimable")
                if now < due: raise JobError("not_due", "scheduled action is not due yet")
                if now > due + ACTION_GRACE:
                    job.update(status="missed", outcome="missed", notice_spoken=False, revision=job["revision"] + 1); notification = "missed"
                else:
                    job.update(status="claimed", claimed_at=iso_utc(now))
            elif job["kind"] == "timer":
                if job["status"] != "due": raise JobError("invalid_state", "timer is not awaiting delivery")
                if now - due > TIMER_GRACE:
                    job.update(status="missed", outcome="undelivered", notice_spoken=False, revision=job["revision"] + 1); notification = "undelivered"
                else: job["status"] = "sounding"
            else: raise JobError("invalid_state", "unknown job kind")
            if job["status"] in {"claimed", "sounding"}: job["revision"] += 1
            result = copy.deepcopy(job); self._remember(request_id, {"job": result})
            await self._save()
            if notification: self.notify(job, notification)
            if job["status"] in {"claimed", "sounding"}: self._schedule_job(job)
            return result

    async def async_timer_delivered(self, request_id: str, job_id: str,
                                    expected_revision: int) -> dict[str, Any]:
        """Record first confirmed output before classifying timer cutoff."""
        async with self._lock:
            replay = self._replay(request_id)
            if replay is not None: return replay["job"]
            job = self._job(job_id); self._revision(job, expected_revision)
            if job.get("delivery", {}).get("owner") == "ha":
                raise JobError("delivery_owned_by_ha", "delivery receipts must come from the bound output")
            if job["kind"] != "timer" or job["status"] != "sounding":
                raise JobError("invalid_state", "timer is not sounding")
            if job.get("delivered_at"):
                return copy.deepcopy(job)
            job.update(delivered_at=iso_utc(self.now()), revision=job["revision"] + 1)
            result = copy.deepcopy(job); self._remember(request_id, {"job": result})
            await self._save()
            return result

    async def async_complete(self, request_id: str, job_id: str, expected_revision: int, outcome: str) -> dict[str, Any]:
        async with self._lock:
            replay = self._replay(request_id)
            if replay is not None: return replay["job"]
            job = self._job(job_id); self._revision(job, expected_revision)
            notify = False
            if job["kind"] == "scheduled_action":
                if job["status"] != "claimed": raise JobError("invalid_state", "scheduled action is not claimed")
                statuses = {"succeeded": "completed", "failed": "failed", "unknown": "unknown", "missed": "missed"}
                if outcome not in statuses: raise JobError("invalid_format", "unsupported scheduled action outcome")
                status, notify = statuses[outcome], outcome in {"failed", "unknown", "missed"}
            else:
                if job.get("delivery", {}).get("owner") == "ha":
                    raise JobError("delivery_owned_by_ha", "HA owns this timer's delivery outcome")
                if job["status"] not in {"due", "sounding"} or outcome != "undelivered":
                    raise JobError("invalid_state", "timer can only complete as undelivered")
                if job.get("delivered_at"):
                    raise JobError("invalid_state", "delivered timer cannot be undelivered")
                status, notify, outcome = "missed", True, "undelivered"
            job.update(status=status, outcome=outcome, revision=job["revision"] + 1)
            if notify: job["notice_spoken"] = False
            result = copy.deepcopy(job); self._remember(request_id, {"job": result})
            await self._save(); self._cancel_schedule(job_id)
            if notify: self.notify(job, outcome)
            return result

    async def async_dismiss_timer(self, request_id: str, job_id: str, expected_revision: int) -> dict[str, Any]:
        async with self._lock:
            replay = self._replay(request_id)
            if replay is not None: return replay["job"]
            job = self._job(job_id); self._revision(job, expected_revision)
            if job["kind"] != "timer" or job["status"] != "sounding":
                raise JobError("invalid_state", "timer is not sounding")
            job.update(status="dismissed", outcome="dismissed", revision=job["revision"] + 1)
            result = copy.deepcopy(job); self._remember(request_id, {"job": result})
            await self._save(); self._cancel_schedule(job_id)
            return result

    async def async_ack_notice(self, request_id: str, job_id: str, expected_revision: int) -> dict[str, Any]:
        """Durably mark one terminal problem notice as spoken by Core."""
        async with self._lock:
            replay = self._replay(request_id)
            if replay is not None: return replay["job"]
            job = self._job(job_id); self._revision(job, expected_revision)
            if job.get("notice_spoken") is not False or job["status"] not in {"failed", "unknown", "missed"}:
                raise JobError("invalid_state", "job has no pending spoken notice")
            job.update(notice_spoken=True, revision=job["revision"] + 1)
            result = copy.deepcopy(job); self._remember(request_id, {"job": result})
            await self._save()
            return result

    async def async_delivery_receipts(self, output: str, receipts: Any) -> list[str]:
        """Commit bound controller evidence before acknowledging its outbox.

        A delivery token identifies a presentation across status revisions. A
        late offline dismissal may correct an undelivered startup classification,
        but cannot undo a cancellation or affect another output/job generation.
        """
        if not isinstance(receipts, list) or len(receipts) > 256:
            raise JobError("invalid_format", "invalid delivery receipt batch")
        async with self._lock:
            original = self.jobs
            updated = copy.deepcopy(original)
            acknowledged, cleared, changed = [], set(), False
            for receipt in receipts:
                if not isinstance(receipt, dict):
                    raise JobError("invalid_format", "receipt must be an object")
                rid, jid, token, event = (receipt.get(k) for k in
                                         ("receipt_id", "job_id", "delivery_token", "event"))
                if any(not isinstance(v, str) or not v or len(v) > 160 for v in (rid, jid, token)):
                    raise JobError("invalid_format", "receipt identity is invalid")
                if event not in {"delivered", "dismissed", "name_played", "name_failed", "name_unknown", "expired"}:
                    raise JobError("invalid_format", "receipt event is invalid")
                at = parse_utc(receipt.get("at"))
                job = updated.get(jid)
                if job is None:
                    if at < self.now() - JOB_RETENTION:
                        acknowledged.append(rid)
                        continue
                    raise JobError("not_found", "receipt job does not exist")
                delivery = job.get("delivery", {})
                if (job["kind"] != "timer" or delivery.get("owner") != "ha"
                        or delivery.get("protocol") == NATIVE_PROTOCOL
                        or delivery.get("output") != output or delivery.get("token") != token):
                    raise JobError("invalid_output", "receipt does not belong to this output/presentation")
                saved = delivery.setdefault("receipts", {})
                evidence = {"event": event, "at": iso_utc(at)}
                if rid in saved:
                    if saved[rid] != evidence:
                        raise JobError("invalid_format", "receipt ID was reused with different data")
                    acknowledged.append(rid)
                    continue
                due, cutoff = parse_utc(job["due_at"]), parse_utc(job["due_at"]) + TIMER_GRACE
                if at < due or at > self.now() + timedelta(seconds=5):
                    raise JobError("invalid_format", "receipt time is outside the presentation")
                # A cutoff can interrupt a name already being fetched/played.
                # Its later failure/uncertainty is honest evidence, never a
                # successful presentation or permission to ring past cutoff.
                if event not in {"expired", "name_unknown", "name_failed"} and at > cutoff:
                    raise JobError("invalid_format", "receipt is after the ringing cutoff")
                saved[rid] = evidence
                acknowledged.append(rid)
                changed = True
                if event == "delivered" and not job.get("delivered_at"):
                    job["delivered_at"] = iso_utc(at)
                    if job["status"] == "due":
                        job["status"] = "sounding"
                    elif job["status"] == "missed" and job.get("outcome") == "undelivered":
                        job.update(status="expired", outcome="auto_stopped", notice_spoken=True)
                        cleared.add(jid)
                elif event == "dismissed" and job["status"] in {"due", "sounding", "missed", "expired"}:
                    job.update(status="dismissed", outcome="dismissed", notice_spoken=True)
                    cleared.add(jid)
                elif event.startswith("name_"):
                    delivery["name_status"] = event.removeprefix("name_")
                elif event == "expired" and job["status"] in {"due", "sounding"}:
                    if job.get("delivered_at"):
                        job.update(status="expired", outcome="auto_stopped")
                    else:
                        # HA's due-time callback owns undelivered notification.
                        continue
                job["revision"] += 1
            if changed:
                self.jobs = updated
                try:
                    await self._save()
                except BaseException:
                    self.jobs = original
                    raise
                for jid in cleared:
                    self.clear_notice(self.jobs[jid])
            return acknowledged

    async def _action_grace_expired(self, job_id: str) -> None:
        async with self._lock:
            job = self.jobs.get(job_id)
            if not job or job["status"] != "pending": return
            if self.now().astimezone(timezone.utc) <= parse_utc(job["due_at"]) + ACTION_GRACE:
                self._schedule_action_grace(job); return
            job.update(status="missed", outcome="missed", notice_spoken=False, revision=job["revision"] + 1)
            await self._save(); self.notify(job, "missed")

    async def _abandoned_claim(self, job_id: str) -> None:
        async with self._lock:
            job = self.jobs.get(job_id)
            if not job or job["kind"] != "scheduled_action" or job["status"] != "claimed": return
            if self.now().astimezone(timezone.utc) <= parse_utc(job["claimed_at"]) + CLAIM_TIMEOUT:
                self._schedule_job(job); return
            job.update(status="unknown", outcome="unknown", notice_spoken=False,
                       revision=job["revision"] + 1)
            await self._save(); self.notify(job, "unknown")

    def _schedule_action_grace(self, job: dict[str, Any]) -> None:
        self._scheduled[job["id"]] = self.schedule_at(
            lambda _now, jid=job["id"]: self.hass.async_create_task(self._action_grace_expired(jid)),
            parse_utc(job["due_at"]) + ACTION_GRACE + timedelta(seconds=1))

    async def _action_due(self, job_id: str) -> None:
        async with self._lock:
            job = self.jobs.get(job_id)
            if not job or job["kind"] != "scheduled_action" or job["status"] != "pending": return
            now, due = self.now().astimezone(timezone.utc), parse_utc(job["due_at"])
            if now > due + ACTION_GRACE:
                job.update(status="missed", outcome="missed", notice_spoken=False, revision=job["revision"] + 1)
                await self._save(); self.notify(job, "missed"); return
            self._emit_due(job)
            self._schedule_action_grace(job)

    async def _timer_due(self, job_id: str) -> None:
        async with self._lock:
            job = self.jobs.get(job_id)
            if not job or job["kind"] != "timer" or job["status"] != "pending": return
            now, due = self.now().astimezone(timezone.utc), parse_utc(job["due_at"])
            if now - due > TIMER_GRACE:
                job.update(status="missed", outcome="undelivered", notice_spoken=False, revision=job["revision"] + 1)
                await self._save(); self.notify(job, "undelivered"); return
            job.update(status="due", revision=job["revision"] + 1)
            await self._save(); self._emit_due(job); self._schedule_job(job)

    async def _timer_expired(self, job_id: str) -> None:
        async with self._lock:
            job = self.jobs.get(job_id)
            if not job or job["kind"] != "timer" or job["status"] not in {"due", "sounding"}: return
            if self.now().astimezone(timezone.utc) <= parse_utc(job["due_at"]) + TIMER_GRACE:
                self._schedule_job(job); return
            if job.get("delivery", {}).get("protocol") == NATIVE_PROTOCOL and job["delivery"].get("finish_attempted_at"):
                job.update(status="unknown", outcome="native_delivery_unknown", notice_spoken=False,
                           revision=job["revision"] + 1)
                await self._save(); self.notify(job, "native_unknown")
            elif job.get("delivered_at"):
                job.update(status="expired", outcome="auto_stopped", revision=job["revision"] + 1)
                await self._save()
            else:
                job.update(status="missed", outcome="undelivered", notice_spoken=False,
                           revision=job["revision"] + 1)
                await self._save(); self.notify(job, "undelivered")
