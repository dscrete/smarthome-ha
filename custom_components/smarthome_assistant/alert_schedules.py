"""Persistent alarm/reminder definitions composed with the SmartHome job store."""
from __future__ import annotations

import copy
import math
import uuid
from datetime import datetime, timedelta, timezone

from .jobs import JobError, TERMINAL_STATES, iso_utc, parse_utc
from .recurrence import RecurrenceError, latest_occurrence, next_occurrence, normalize_schedule

ALARM_GRACE = timedelta(minutes=5)
MAX_MESSAGE = 1000


def _id():
    return str(uuid.uuid4())


def _normalize(manager, raw):
    try:
        return normalize_schedule(raw, now=manager.now().astimezone(timezone.utc),
                                  default_timezone=getattr(getattr(manager.hass, "config", None), "time_zone", "UTC") or "UTC")
    except RecurrenceError as error:
        raise JobError("invalid_format", str(error)) from error


async def create(manager, request_id, raw):
    if not isinstance(raw, dict) or raw.get("kind") not in {"alarm", "reminder"}:
        raise JobError("invalid_format", "kind must be alarm or reminder")
    if set(raw) - {"kind", "schedule", "payload", "name", "source_satellite", "context_satellite"}:
        raise JobError("invalid_format", "alert contains unsupported fields")
    async with manager._lock:
        replay = manager._replay(request_id)
        if replay is not None:
            return replay["result"]
        now = manager.now().astimezone(timezone.utc)
        kind = raw["kind"]
        schedule = _normalize(manager, raw.get("schedule"))
        payload = raw.get("payload", {})
        if not isinstance(payload, dict):
            raise JobError("invalid_format", "payload must be an object")
        if kind == "reminder":
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
                raise JobError("invalid_format", "reminder payload requires message text of at most 1000 characters")
            payload = {"message": message.strip()}
        elif payload:
            raise JobError("invalid_format", "alarms do not accept a payload")
        name = raw.get("name")
        if name is not None and (not isinstance(name, str) or len(name) > 160):
            raise JobError("invalid_format", "name must be a string of at most 160 characters")
        requested_source = raw.get("source_satellite")
        if requested_source is not None and (not isinstance(requested_source, str) or len(requested_source) > 160):
            raise JobError("invalid_format", "source_satellite must be a string of at most 160 characters")
        context_source = raw.get("context_satellite")
        if context_source is not None and (not isinstance(context_source, str) or len(context_source) > 160):
            raise JobError("invalid_format", "context_satellite must be a string of at most 160 characters")
    if not manager.alert_output:
        raise JobError("unavailable", "HA alert output resolution is unavailable")
    source = (requested_source if requested_source is not None else context_source
              if context_source is not None else manager.default_alert_output or None)
    output = await manager.alert_output(source, kind)
    due = (parse_utc(schedule["due_at"]) if schedule["kind"] == "once"
           else next_occurrence(schedule, now))
    if due is None:
        raise JobError("invalid_format", "schedule has no occurrence")
    async with manager._lock:
        replay = manager._replay(request_id)
        if replay is not None:
            return replay["result"]
        now = manager.now().astimezone(timezone.utc)
        schedule_id, job_id = _id(), _id()
        record = {"id": schedule_id, "kind": kind, "name": name, "payload": payload,
                  "schedule": schedule, "enabled": True, "revision": 1,
                  "source_satellite": output["output"], "context_source": context_source,
                  "delivery": _delivery(output),
                  "created_at": iso_utc(now), "next_due_at": iso_utc(next_occurrence(schedule, due))
                  if _has_next(schedule) else None, "current_job_id": job_id}
        job = _occurrence(record, job_id, due, now)
        manager.schedules[schedule_id] = record
        manager.jobs[job_id] = job
        result = {"job": copy.deepcopy(job), "schedule": copy.deepcopy(record)}
        manager._remember(request_id, {"result": result})
        await manager._save()
        manager._schedule_job(job)
        return result


def _has_next(schedule):
    return schedule["kind"] != "once"


def _delivery(output):
    delivery = {"owner": "ha", "output": output["output"], "token": _id(), "receipts": {}}
    if output.get("delivery_protocol") == "assist_native":
        delivery.update(protocol="assist_native", device_id=output["device_id"])
    elif output.get("delivery_protocol"):
        delivery["protocol"] = output["delivery_protocol"]
    for key in ("mac_address", "controller_device_id", "device_id"):
        if output.get(key):
            delivery[key] = output[key].replace(":", "").replace("-", "").lower() if key == "mac_address" else output[key]
    return delivery


def _occurrence(schedule, job_id, due, now, *, original_due=None, snoozed_from=None):
    kind = schedule["kind"]
    remaining = max(1, math.ceil((due - now).total_seconds()))
    job = {"id": job_id, "kind": kind, "due_at": iso_utc(due),
           "payload": ({"duration_seconds": remaining} if kind == "alarm" else copy.deepcopy(schedule["payload"])),
           "source_satellite": schedule["source_satellite"], "name": schedule.get("name"),
           "context_source": schedule.get("context_source"),
           "status": "pending", "revision": 1, "created_at": iso_utc(now),
           "schedule_id": schedule["id"], "delivery": copy.deepcopy(schedule["delivery"])}
    job["delivery"]["token"] = _id()
    job["delivery"]["receipts"] = {}
    if original_due is not None:
        job["original_due_at"] = iso_utc(original_due)
    if snoozed_from:
        job["snoozed_from"] = snoozed_from
    return job


def list_schedules(manager, *, kinds=None, enabled=None):
    values = [copy.deepcopy(value) for value in manager.schedules.values()
              if (kinds is None or value["kind"] in kinds)
              and (enabled is None or value["enabled"] is enabled)]
    now = manager.now().astimezone(timezone.utc)
    for schedule in values:
        schedule["allowed_operations"] = ["disable", "edit", "cancel"] if schedule["enabled"] else ["enable", "edit", "cancel"]
        current = manager.jobs.get(schedule.get("current_job_id"))
        if current:
            schedule["current_job"] = copy.deepcopy(current)
            schedule["current_job"]["allowed_operations"] = _allowed_job_ops(current, now)
            schedule["current_job"]["delivery_guarantee"] = manager.delivery_guarantee(current)
            schedule["current_job"]["delivery_facts"] = manager.delivery_facts(current)
        schedule["updated_at"] = schedule.get("updated_at", schedule.get("created_at"))
        if schedule.get("next_due_at"):
            schedule["remaining_seconds"] = max(0, math.ceil((parse_utc(schedule["next_due_at"]) - now).total_seconds()))
    return values


async def resolve(manager, request):
    if not isinstance(request, dict):
        raise JobError("invalid_format", "resolve request must be an object")
    request = copy.deepcopy(request)
    typed = request.get("selector")
    if isinstance(typed, dict):
        if "kind" not in typed:
            raise JobError("invalid_format", "typed selector requires kind")
        selector_fields = {"name": {"name"}, "duration": {"duration_seconds"},
                           "recent": set(), "automatic": set(), "id": {"job_id", "schedule_id"}}
        if (not isinstance(typed["kind"], str) or typed["kind"] not in selector_fields
                or set(typed) - {"kind", "area_id"} - selector_fields[typed["kind"]]):
            raise JobError("invalid_format", "unsupported typed selector fields")
        for key, value in typed.items():
            if key == "kind":
                continue
            if key in request and request[key] != value:
                raise JobError("invalid_format", "conflicting selector fields")
            request[key] = value
        request["selector"] = typed["kind"]
    allowed = {"target_type", "selector", "name", "duration_seconds", "job_id", "schedule_id",
               "area_id", "source_satellite", "recent_id", "kinds", "operation"}
    if set(request) - allowed:
        raise JobError("invalid_format", "resolve request contains unsupported fields")
    target_type = request.get("target_type", "job")
    if target_type not in {"job", "schedule"}:
        raise JobError("invalid_format", "target_type must be job or schedule")
    selector = request.get("selector", "automatic")
    if not isinstance(selector, str) or selector not in {"name", "duration", "recent", "automatic", "id"}:
        raise JobError("invalid_format", "unsupported selector")
    kinds = request.get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or any(kind not in {"timer", "alarm", "reminder", "scheduled_action"} for kind in kinds)):
        raise JobError("invalid_format", "kinds must be supported job kinds")
    operation = request.get("operation")
    if operation is not None and not isinstance(operation, str):
        raise JobError("invalid_format", "operation must be a string")
    source = request.get("source_satellite")
    resolved_source = None
    if source is not None:
        try:
            resolver = getattr(manager, "alert_output", None)
            if resolver is not None:
                source_kind = "reminder" if kinds == ["reminder"] else "alarm"
                resolved_source = (await resolver(source, source_kind))["output"]
            else:
                resolved_source = source
        except JobError as error:
            if error.code == "ambiguous_output":
                return {"status": "ambiguous", "choices": [
                    {"source_satellite": item["entity_id"], "name": item["name"],
                     "area_id": item.get("area_id"), "area_name": item.get("area_name")}
                    for item in getattr(error, "choices", [])]}
            if error.code in {"unsupported_output", "unavailable"}:
                resolved_source = source
            else:
                raise
    async with manager._lock:
        now = manager.now().astimezone(timezone.utc)
        if target_type == "job":
            records = [copy.deepcopy(job) for job in manager.jobs.values()
                       if kinds is None or job["kind"] in kinds]
            records = [job for job in records if _resolve_operation(job, operation, now)]
            id_key = "job_id"
            for job in records:
                job["allowed_operations"] = _allowed_job_ops(job, now)
                job["delivery_facts"] = manager.delivery_facts(job)
        else:
            records = [copy.deepcopy(item) for item in manager.schedules.values()
                       if kinds is None or item["kind"] in kinds]
            records = [item for item in records if _allowed_schedule_ops(item, operation)]
            id_key = "schedule_id"
        area_id = request.get("area_id")
        recent_matched = False
        if area_id is not None:
            if not isinstance(area_id, str) or not area_id.strip():
                raise JobError("invalid_format", "area_id must be non-empty")
            area = area_id.strip().casefold()
            catalog = manager.output_catalog() if manager.output_catalog else []
            area_outputs = {item["entity_id"] for item in catalog if area in {
                str(item.get("area_id") or "").casefold(), str(item.get("area_name") or "").casefold()}}
            records = [record for record in records if record.get("source_satellite") in area_outputs
                       or area in {str(record.get("payload", {}).get("area_id") or "").casefold(),
                                   str(record.get("payload", {}).get("area_name") or "").casefold()}]
        if selector == "id":
            identifier = request.get(id_key)
            if not isinstance(identifier, str) or not identifier:
                raise JobError("invalid_format", f"id selector requires {id_key}")
            records = [record for record in records if record["id"] == identifier]
        elif selector == "name":
            name = request.get("name")
            if not isinstance(name, str) or not name.strip():
                raise JobError("invalid_format", "name selector requires name")
            records = [record for record in records if (record.get("name") or "").casefold() == name.strip().casefold()]
        elif selector == "duration":
            seconds = request.get("duration_seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
                raise JobError("invalid_format", "duration selector requires positive duration_seconds")
            records = [record for record in records if record.get("payload", {}).get("duration_seconds") == seconds]
        elif selector == "recent":
            recent = request.get("recent_id")
            records = [record for record in records if record["id"] == recent]
        elif selector == "automatic" and request.get("recent_id"):
            recent = request["recent_id"]
            preferred = [record for record in records if record["id"] == recent]
            if preferred:
                records = preferred
                recent_matched = True
        # Source is a preference after explicit identity/room and recent context.
        # A missing explicit identity or room never drops its filter.
        if len(records) > 1 or (selector == "automatic" and not area_id and not recent_matched):
            if resolved_source is not None:
                local = [record for record in records if record.get("source_satellite") == resolved_source
                         or record.get("context_source") == source
                         or record.get("payload", {}).get("context_source") == source]
                if local or (selector == "automatic" and not area_id):
                    records = local
        if not records:
            return {"status": "no_match", "choices": []}
        if len(records) == 1:
            record = records[0]
            if target_type == "job" and record["kind"] == "timer":
                record["remaining_seconds"] = (record.get("paused_remaining_seconds") if record["status"] == "paused"
                    else max(0, math.ceil((parse_utc(record["due_at"]) - now).total_seconds())))
            return {"status": "selected", "choice": _choice(record, target_type), target_type: record}
        return {"status": "ambiguous", "choices": [_choice(record, target_type) for record in records]}


def _resolve_operation(job, operation, now):
    if operation is None or operation == "query":
        return (job["status"] in {"pending", "paused", "due", "sounding"}
                or job["kind"] == "alarm" and job.get("outcome") == "handed_off"
                and now <= parse_utc(job["due_at"]) + ALARM_GRACE)
    if operation == "cancel" and job["kind"] == "timer" and job["status"] == "sounding":
        return True
    return operation in _allowed_job_ops(job, now)


def _allowed_job_ops(job, now):
    if job["kind"] == "timer":
        from .jobs import JobsManager
        allowed = JobsManager._allowed_timer_operations(job, now)
        if job["status"] in {"pending", "paused", "due", "sounding"}:
            allowed.append("query")
        return allowed
    if job["kind"] == "alarm" and job["status"] in {"due", "sounding", "completed"} and now <= parse_utc(job["due_at"]) + ALARM_GRACE:
        if job["status"] != "completed" or job.get("outcome") == "handed_off":
            return ["snooze", "dismiss"]
    return []


def _allowed_schedule_ops(schedule, operation):
    if operation == "query":
        return True
    allowed = (["disable", "edit", "cancel"] if schedule.get("enabled")
               else ["enable", "edit", "cancel"])
    return operation is None or operation in allowed


def _choice(record, target_type):
    key = "schedule_id" if target_type == "schedule" else "job_id"
    return {"id": record["id"], key: record["id"], "kind": record["kind"], "name": record.get("name"),
            "revision": record["revision"], "status": record.get("status"),
            "source_satellite": record.get("source_satellite"),
            "due_at": record.get("due_at"),
            "duration_seconds": record.get("payload", {}).get("duration_seconds"),
            **({"schedule_id": record.get("schedule_id")} if target_type == "job" else {})}


async def outputs(manager):
    return {"outputs": manager.output_catalog() if manager.output_catalog else [],
            "default_alert_output": manager.default_alert_output,
            "snooze_seconds": manager.snooze_seconds,
            "timezone": getattr(getattr(manager.hass, "config", None), "time_zone", "UTC") or "UTC"}


async def operate(manager, request_id, *, schedule_id=None, job_id=None, expected_revision, operation, arguments=None):
    args = {} if arguments is None else arguments
    if not isinstance(args, dict):
        raise JobError("invalid_format", "arguments must be an object")
    if bool(schedule_id) == bool(job_id):
        raise JobError("invalid_format", "provide exactly one of schedule_id or job_id")
    if schedule_id:
        return await _operate_schedule(manager, request_id, schedule_id, expected_revision, operation, args)
    return await _operate_occurrence(manager, request_id, job_id, expected_revision, operation, args)


async def _operate_schedule(manager, request_id, schedule_id, expected_revision, operation, args):
    resolved_output = None
    if operation == "edit" and "source_satellite" in args:
        async with manager._lock:
            replay = manager._replay(request_id)
            if replay is not None:
                return replay["result"]
            original = manager.schedules.get(schedule_id)
            if original is None:
                raise JobError("not_found", "schedule does not exist")
            manager._revision(original, expected_revision)
            kind = original["kind"]
        if not isinstance(args["source_satellite"], str) or not args["source_satellite"]:
            raise JobError("invalid_format", "source_satellite must be a non-empty registered output")
        if not manager.alert_output:
            raise JobError("unavailable", "HA alert output resolution is unavailable")
        resolved_output = await manager.alert_output(args["source_satellite"], kind)
    async with manager._lock:
        replay = manager._replay(request_id)
        if replay is not None:
            return replay["result"]
        original_jobs, original_schedules = manager.jobs, manager.schedules
        manager.jobs, manager.schedules = copy.deepcopy(manager.jobs), copy.deepcopy(manager.schedules)
        schedule = manager.schedules.get(schedule_id)
        if schedule is None:
            manager.jobs, manager.schedules = original_jobs, original_schedules
            raise JobError("not_found", "schedule does not exist")
        try:
            manager._revision(schedule, expected_revision)
        except BaseException:
            manager.jobs, manager.schedules = original_jobs, original_schedules
            raise
        now = manager.now().astimezone(timezone.utc)
        cancel_ids = []
        handoff_ids = []
        try:
            current = manager.jobs.get(schedule.get("current_job_id"))
            if operation in {"cancel", "disable"}:
                if args or (operation == "disable" and not schedule["enabled"]):
                    raise JobError("invalid_state", "schedule is already disabled or arguments are invalid")
                schedule["enabled"] = False
                schedule["next_due_at"] = None
                if operation == "cancel":
                    schedule["finished_at"] = iso_utc(now)
                for occurrence in manager.jobs.values():
                    if occurrence.get("schedule_id") != schedule_id:
                        continue
                    if occurrence["status"] not in TERMINAL_STATES:
                        _stop_job(occurrence, "cancelled", "schedule_cancelled" if operation == "cancel" else "schedule_disabled")
                        cancel_ids.append(occurrence["id"])
                    elif _active_to_stop(occurrence, now):
                        occurrence["delivery"]["stop_requested_at"] = iso_utc(now)
                        occurrence["delivery"]["stop_outcome"] = "requested_stop_unconfirmed"
                        occurrence["prior_outcome"] = occurrence.get("outcome")
                        handoff_ids.append(occurrence["id"])
            elif operation == "enable":
                if args or schedule["enabled"]:
                    raise JobError("invalid_state", "schedule is already enabled or arguments are invalid")
                schedule["enabled"] = True
                schedule.pop("finished_at", None)
                _spawn_after_now(manager, schedule, now)
            elif operation == "edit":
                if not args or set(args) - {"name", "schedule", "payload", "source_satellite", "context_satellite"}:
                    raise JobError("invalid_format", "edit supports name, schedule, payload and output/source selectors")
                # Validate every field before changing the current occurrence.
                new_name = schedule.get("name")
                if "name" in args:
                    new_name = args["name"]
                    if new_name is not None and (not isinstance(new_name, str) or len(new_name) > 160):
                        raise JobError("invalid_format", "name must be a string of at most 160 characters")
                new_payload = schedule["payload"]
                if "payload" in args:
                    payload = args["payload"]
                    if schedule["kind"] == "reminder":
                        message = payload.get("message") if isinstance(payload, dict) else None
                        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
                            raise JobError("invalid_format", "reminder payload requires message text")
                        new_payload = {"message": message.strip()}
                    elif payload:
                        raise JobError("invalid_format", "alarms do not accept a payload")
                new_recurrence = _normalize(manager, args["schedule"]) if "schedule" in args else schedule["schedule"]
                new_context = schedule.get("context_source")
                if "context_satellite" in args:
                    new_context = args["context_satellite"]
                    if new_context is not None and (not isinstance(new_context, str) or len(new_context) > 160):
                        raise JobError("invalid_format", "context_satellite must be a string of at most 160 characters")
                was_enabled = schedule["enabled"]
                schedule.update(name=new_name, payload=new_payload, schedule=new_recurrence)
                schedule["context_source"] = new_context
                if resolved_output:
                    schedule["source_satellite"] = resolved_output["output"]
                    schedule["delivery"] = _delivery(resolved_output)
                next_regular = (next_occurrence(new_recurrence, now) if was_enabled
                                and (_has_next(new_recurrence) or current and current.get("snoozed_from")) else None)
                schedule["next_due_at"] = iso_utc(next_regular) if next_regular else None
                # A snoozed occurrence is a user-directed, already accepted
                # generation. Editing the definition changes future regular
                # occurrences, but must not replace its due time or delivery
                # identity. The definition callback below will still supersede
                # it when the edited regular occurrence arrives.
                if (was_enabled and current and current["status"] == "pending"
                        and current.get("snoozed_from")):
                    pass
                elif (was_enabled and current and current["status"] == "pending"
                        and parse_utc(current["due_at"]) > now):
                    _stop_job(current, "cancelled", "schedule_edited")
                    cancel_ids.append(current["id"])
                    schedule["current_job_id"] = None
                    _spawn_after_now(manager, schedule, now)
                elif was_enabled and (not current or current["status"] in TERMINAL_STATES):
                    _spawn_after_now(manager, schedule, now)
            else:
                raise JobError("invalid_format", "unsupported schedule operation")
            schedule["revision"] += 1
            schedule["updated_at"] = iso_utc(now)
            result = {"schedule": copy.deepcopy(schedule)}
            manager._remember(request_id, {"result": result})
            await manager._save()
        except BaseException:
            manager.jobs, manager.schedules = original_jobs, original_schedules
            raise
        for old_id in cancel_ids:
            manager._cancel_schedule(old_id)
        for old_id in handoff_ids:
            occurrence = manager.jobs[old_id]
            try:
                stopped = bool(manager.alert_stop and manager.alert_stop(copy.deepcopy(occurrence)))
            except Exception:
                stopped = False
            occurrence["delivery"]["stop_outcome"] = "submitted" if stopped else "requested_stop_unconfirmed"
            try:
                await manager._save()
            except Exception:
                # Stop outcome remains uncertain; the durable request above is authoritative.
                pass
        current = manager.jobs.get(schedule.get("current_job_id"))
        if current and current["status"] == "pending":
            manager._schedule_job(current)
        if operation == "edit":
            manager._schedule_definition(schedule)
        return result


def _active_to_stop(job, now):
    if job["status"] not in TERMINAL_STATES:
        return True
    return (job["kind"] == "alarm" and job["status"] == "completed"
            and now <= parse_utc(job["due_at"]) + ALARM_GRACE
            and job.get("outcome") == "handed_off")


def _spawn_after_now(manager, schedule, now):
    due = next_occurrence(schedule["schedule"], now)
    if due is None:
        schedule["enabled"] = False
        schedule["next_due_at"] = None
        schedule["current_job_id"] = None
        return
    job_id = _id()
    job = _occurrence(schedule, job_id, due, now)
    schedule["current_job_id"] = job_id
    schedule["next_due_at"] = iso_utc(next_occurrence(schedule["schedule"], due)) if _has_next(schedule["schedule"]) else None
    manager.jobs[job_id] = job


async def _operate_occurrence(manager, request_id, job_id, expected_revision, operation, args):
    async with manager._lock:
        replay = manager._replay(request_id)
        if replay is not None:
            return replay["result"]
        original = manager._job(job_id)
        manager._revision(original, expected_revision)
        if original["kind"] not in {"alarm", "reminder"}:
            raise JobError("invalid_state", "job is not an alarm or reminder occurrence")
        now = manager.now().astimezone(timezone.utc)
        if operation not in {"dismiss", "snooze"} or original["kind"] != "alarm":
            raise JobError("invalid_format", "unsupported occurrence operation")
        if operation == "dismiss" and args:
            raise JobError("invalid_format", "dismiss does not accept arguments")
        seconds = manager.snooze_seconds
        if operation == "snooze":
            if set(args) - {"seconds"}:
                raise JobError("invalid_format", "snooze supports only seconds")
            seconds = args.get("seconds", seconds)
            if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 86400:
                raise JobError("invalid_format", "snooze seconds must be a whole value from 1 to 86400")
        allowed_status = {"due", "sounding", "completed"}
        if original["status"] not in allowed_status or now > parse_utc(original["due_at"]) + ALARM_GRACE:
            raise JobError("invalid_state", "alarm is no longer sounding and cannot be changed")
        if original["status"] == "completed" and original.get("outcome") != "handed_off":
            raise JobError("invalid_state", "completed alarm has no active handoff")
        original_jobs, original_schedules = manager.jobs, manager.schedules
        manager.jobs, manager.schedules = copy.deepcopy(manager.jobs), copy.deepcopy(manager.schedules)
        job = manager.jobs[job_id]
        schedule = manager.schedules.get(job.get("schedule_id"))
        try:
            result_job = job
            if operation == "dismiss":
                prior = job.get("outcome")
                _stop_job(job, "dismissed", "dismissed")
                if prior:
                    job["prior_outcome"] = prior
                finish_occurrence(manager, job, now)
            else:
                snoozed_due = now + timedelta(seconds=seconds)
                regular_due = parse_utc(schedule["next_due_at"]) if schedule and schedule.get("next_due_at") else None
                following = manager.jobs.get(schedule.get("current_job_id")) if schedule else None
                if (following and following["id"] != job_id and following["status"] == "pending"
                        and not following.get("snoozed_from")):
                    regular_due = parse_utc(following["due_at"])
                prior = job.get("outcome")
                _stop_job(job, "cancelled", "snoozed")
                if prior:
                    job["prior_outcome"] = prior
                if regular_due and snoozed_due >= regular_due:
                    job["outcome"] = "snoozed_superseded"
                    result_job = (following if following and following["id"] != job_id
                                  and following["status"] == "pending"
                                  else _spawn_due(manager, schedule, regular_due, now, original_due=regular_due))
                else:
                    if following and following["id"] != job_id and following["status"] == "pending":
                        _stop_job(following, "cancelled", "snooze_replaced_pending")
                    base = schedule or {**job, "id": job.get("schedule_id"), "payload": {},
                                        "delivery": job["delivery"]}
                    result_job = _occurrence(base, _id(), snoozed_due, now,
                                             original_due=parse_utc(job.get("original_due_at", job["due_at"])),
                                             snoozed_from=job_id)
                    manager.jobs[result_job["id"]] = result_job
                    if schedule:
                        schedule["enabled"] = True
                        schedule.pop("finished_at", None)
                        schedule["current_job_id"] = result_job["id"]
                        schedule["next_due_at"] = iso_utc(regular_due) if regular_due else None
                        schedule["revision"] += 1
            result = {"job": copy.deepcopy(result_job), "previous_job": copy.deepcopy(job),
                      "schedule": copy.deepcopy(schedule) if schedule else None,
                      "delivery_facts": manager.delivery_facts(job)}
            result["job"]["delivery_facts"] = manager.delivery_facts(result_job)
            result["previous_job"]["delivery_facts"] = manager.delivery_facts(job)
            manager._remember(request_id, {"result": result})
            await manager._save()
        except BaseException:
            manager.jobs, manager.schedules = original_jobs, original_schedules
            raise
        manager._cancel_schedule(job_id)
        if operation == "snooze" and following and following["id"] != result_job["id"] and following["id"] != job_id:
            manager._cancel_schedule(following["id"])
        if schedule:
            current = manager.jobs.get(schedule.get("current_job_id"))
            if current and current["status"] == "pending":
                manager._schedule_job(current)
        return result


def _stop_job(job, status, outcome):
    if job["kind"] == "reminder" and job.get("delivery", {}).get("alert_attempted_at"):
        # Cancelling the definition cannot retract speech already handed to HA.
        # Retain that uncertainty even if the outstanding service call is lost.
        job.update(status="unknown", outcome="announcement_unknown", prior_outcome=outcome,
                   notice_spoken=False, finished_at=None, revision=job["revision"] + 1)
    else:
        job.update(status=status, outcome=outcome, finished_at=None, revision=job["revision"] + 1)


def _spawn_due(manager, schedule, due, now, *, original_due=None):
    if not schedule:
        return None
    job = _occurrence(schedule, _id(), due, now, original_due=original_due)
    manager.jobs[job["id"]] = job
    schedule["current_job_id"] = job["id"]
    schedule["next_due_at"] = iso_utc(next_occurrence(schedule["schedule"], due)) if _has_next(schedule["schedule"]) else None
    schedule["revision"] += 1
    return job


def _finish_schedule(manager, schedule, now):
    if not schedule or not schedule.get("enabled"):
        return
    next_at = schedule.get("next_due_at")
    if not next_at:
        schedule["enabled"] = False
        schedule["current_job_id"] = None
        schedule.setdefault("finished_at", iso_utc(now))
        schedule["revision"] += 1
        return
    due = parse_utc(next_at)
    if due <= now and schedule["kind"] == "reminder":
        try:
            due = latest_occurrence(schedule["schedule"], due, now)
        except RecurrenceError as error:
            raise JobError("invalid_state", str(error)) from error
    if due <= now and schedule["kind"] == "alarm":
        try:
            # Skip elapsed regular occurrences as a group, but retain the
            # latest one when it is still inside the alarm delivery window.
            due = latest_occurrence(schedule["schedule"], due, now)
        except RecurrenceError as error:
            raise JobError("invalid_state", str(error)) from error
        if due is None:
            due = next_occurrence(schedule["schedule"], now)
        elif now - due > ALARM_GRACE:
            missed = _occurrence(schedule, _id(), due, now, original_due=due)
            missed.update(status="missed", outcome="missed", notice_spoken=False)
            manager.jobs[missed["id"]] = missed
            due = next_occurrence(schedule["schedule"], now)
            if due is None:
                schedule["enabled"] = False
                schedule["next_due_at"] = None
                schedule["current_job_id"] = None
                schedule.setdefault("finished_at", iso_utc(now))
                schedule["revision"] += 1
                return
            _spawn_due(manager, schedule, due, now, original_due=due)
            return
        if due is None:
            schedule["enabled"] = False
            schedule["next_due_at"] = None
            schedule["current_job_id"] = None
            schedule.setdefault("finished_at", iso_utc(now))
            schedule["revision"] += 1
            return
        _spawn_due(manager, schedule, due, now, original_due=due)
        return
    _spawn_due(manager, schedule, due, now, original_due=parse_utc(next_at))


def finish_occurrence(manager, job, now):
    """Advance one recurring definition after its current occurrence is terminal."""
    schedule = manager.schedules.get(job.get("schedule_id"))
    if schedule and schedule.get("current_job_id") == job["id"]:
        _finish_schedule(manager, schedule, now)


async def regular_due(manager, schedule_id):
    """Supersede old alerts at the next calendar occurrence, without a backlog."""
    async with manager._lock:
        schedule = manager.schedules.get(schedule_id)
        now = manager.now().astimezone(timezone.utc)
        if not schedule or not schedule.get("enabled") or not schedule.get("next_due_at"):
            return
        first = parse_utc(schedule["next_due_at"])
        if first > now:
            manager._schedule_definition(schedule)
            return
        latest = latest_occurrence(schedule["schedule"], first, now)
        current = manager.jobs.get(schedule.get("current_job_id"))
        if (current and not current.get("snoozed_from") and parse_utc(current["due_at"]) == latest
                and current["status"] in {"pending", "due", "sounding"}):
            schedule["next_due_at"] = iso_utc(next_occurrence(schedule["schedule"], latest))
            schedule["revision"] += 1
            await manager._save()
            return
        cancel_ids = []
        original_due = first
        for old in manager.jobs.values():
            if old.get("schedule_id") != schedule_id or not _active_to_stop(old, now):
                continue
            if old["kind"] == "reminder":
                original_due = min(original_due, parse_utc(old.get("original_due_at", old["due_at"])))
            prior = old.get("outcome")
            _stop_job(old, "cancelled", "superseded")
            if prior:
                old["prior_outcome"] = prior
            cancel_ids.append(old["id"])
        occurrence = _spawn_due(manager, schedule, latest, now, original_due=original_due)
        missed = occurrence["kind"] == "alarm" and now - latest > ALARM_GRACE
        if missed:
            occurrence.update(status="missed", outcome="alarm_missed", notice_spoken=False)
            _finish_schedule(manager, schedule, now)
        await manager._save()
        for job_id in cancel_ids:
            manager._cancel_schedule(job_id)
        current = manager.jobs.get(schedule.get("current_job_id"))
        if current:
            manager._schedule_job(current)
        if missed:
            manager.notify(occurrence, "alarm_missed")


async def reconcile_startup(manager):
    async with manager._lock:
        now = manager.now().astimezone(timezone.utc)
        changed = False
        for schedule in manager.schedules.values():
            current = manager.jobs.get(schedule.get("current_job_id"))
            if current and current["status"] in TERMINAL_STATES:
                continue
            if not schedule.get("enabled") or not current:
                continue
            due = parse_utc(current["due_at"])
            if current.get("delivery", {}).get("alert_attempted_at"):
                current.update(status="unknown", outcome="announcement_unknown", notice_spoken=False,
                               revision=current["revision"] + 1)
                changed = True
                _finish_schedule(manager, schedule, now)
                continue
            if schedule["kind"] == "alarm":
                if due <= now and now - due > ALARM_GRACE:
                    current.update(status="missed", outcome="missed", notice_spoken=False,
                                   revision=current["revision"] + 1)
                    changed = True
                    _finish_schedule(manager, schedule, now)
                else:
                    if due <= now:
                        current.update(status="due", revision=current["revision"] + 1)
                        changed = True
                    manager._schedule_job(current)
            else:
                if due <= now and schedule["schedule"]["kind"] != "once":
                    try:
                        latest = latest_occurrence(schedule["schedule"], due, now)
                    except RecurrenceError as error:
                        raise JobError("invalid_state", str(error)) from error
                    if latest and latest > due:
                        current["original_due_at"] = current.get("original_due_at", current["due_at"])
                        current["due_at"] = iso_utc(latest)
                        current["missed_occurrences"] = max(0, (latest - due).days)
                        current["revision"] += 1
                        changed = True
                manager._schedule_job(current)
                if due <= now and current["status"] == "pending":
                    current.update(status="due", revision=current["revision"] + 1)
                    changed = True
        if changed:
            await manager._save()
            for schedule in manager.schedules.values():
                current = manager.jobs.get(schedule.get("current_job_id"))
                if current and current["status"] == "pending":
                    manager._schedule_job(current)
            for job in manager.jobs.values():
                if job.get("notice_spoken") is False and job.get("schedule_id") in manager.schedules:
                    reason = ("alarm_unknown" if job["kind"] == "alarm" and job.get("outcome") == "announcement_unknown"
                              else "reminder_unknown" if job["kind"] == "reminder" and job.get("outcome") == "announcement_unknown"
                              else "alarm_missed")
                    manager.notify(job, reason)


async def due(manager, job_id):
    async with manager._lock:
        job = manager.jobs.get(job_id)
        if not job or job["kind"] not in {"alarm", "reminder"} or job["status"] != "pending":
            return
        now, due_at = manager.now().astimezone(timezone.utc), parse_utc(job["due_at"])
        superseded = []
        for old in manager.jobs.values():
            if (old["id"] == job_id or old.get("schedule_id") != job.get("schedule_id")
                    or not _active_to_stop(old, now) or parse_utc(old["due_at"]) > due_at):
                continue
            prior = old.get("outcome")
            _stop_job(old, "cancelled", "superseded")
            if prior:
                old["prior_outcome"] = prior
            superseded.append(old["id"])
        if job["kind"] == "alarm" and now - due_at > ALARM_GRACE:
            job.update(status="missed", outcome="missed", notice_spoken=False, revision=job["revision"] + 1)
            _finish_schedule(manager, manager.schedules.get(job.get("schedule_id")), now)
            await manager._save()
            for old_id in superseded:
                manager._cancel_schedule(old_id)
            manager._cancel_schedule(job_id)
            manager._schedule_following_alert(job)
            return
        job.update(status="due", revision=job["revision"] + 1)
        await manager._save()
        for old_id in superseded:
            manager._cancel_schedule(old_id)
        manager._emit_due(job)


async def reminder_reserve(manager, job_id, expected_revision):
    async with manager._lock:
        job = manager.jobs.get(job_id)
        if (not job or job["kind"] != "reminder" or job["status"] != "due"
                or job["revision"] != expected_revision
                or job.get("delivery", {}).get("alert_attempted_at")):
            return None
        job["delivery"]["alert_attempted_at"] = iso_utc(manager.now())
        job["revision"] += 1
        await manager._save()
        return copy.deepcopy(job)


async def reminder_finish(manager, job_id, expected_revision, error=None):
    async with manager._lock:
        job = manager.jobs.get(job_id)
        if (not job or job["kind"] != "reminder" or job["revision"] != expected_revision
                or not job.get("delivery", {}).get("alert_attempted_at")):
            return False
        if error is None:
            job.update(status="completed", outcome="announced")
        else:
            job.update(status="unknown", outcome="announcement_unknown", notice_spoken=False)
        job["revision"] += 1
        schedule = manager.schedules.get(job.get("schedule_id"))
        _finish_schedule(manager, schedule, manager.now().astimezone(timezone.utc))
        await manager._save()
        manager._cancel_schedule(job_id)
        manager._schedule_following_alert(job)
        if error is not None:
            manager.notify(job, "reminder_unknown")
        return error is None
