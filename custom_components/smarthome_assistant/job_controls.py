"""HA-owned atomic controls shared by timer clients."""
from __future__ import annotations

import copy
from datetime import timezone

from .jobs import ACTIVE_TIMER_STATUSES, NATIVE_PROTOCOL, TIMER_GRACE, JobError, parse_utc


def delivery_facts(job: dict) -> dict:
    """Report silence only when a validated controller receipt proves it."""
    delivery = job.get("delivery") or {}
    receipts = delivery.get("receipts") or {}
    due = None
    try:
        due = parse_utc(job.get("due_at"))
    except JobError:
        pass
    stop_receipt = (delivery.get("owner") == "ha"
                    and delivery.get("protocol") != NATIVE_PROTOCOL
                    and isinstance(delivery.get("output"), str)
                    and isinstance(delivery.get("token"), str)
                    and due is not None
                    and any(isinstance(item, dict)
                            and ((item.get("event") == "dismissed" and _receipt_in_window(item, due))
                                 or (item.get("event") == "expired" and _expired_receipt(item, due)))
                            for item in receipts.values()))
    return {"stop_confirmed": bool(stop_receipt)}


def _receipt_in_window(receipt, due):
    try:
        at = parse_utc(receipt.get("at"))
    except JobError:
        return False
    return due <= at <= due + TIMER_GRACE


def _expired_receipt(receipt, due):
    try:
        at = parse_utc(receipt.get("at"))
    except JobError:
        return False
    return at >= due


def _source_member(job: dict, source_satellite: str | None, context_source: str | None) -> bool:
    stored_context = (job.get("payload") or {}).get("context_source")
    if isinstance(stored_context, str) and stored_context:
        return context_source is not None and stored_context == context_source
    if source_satellite is None:
        return False
    delivery = job.get("delivery") or {}
    identities = {job.get("source_satellite"), delivery.get("output"), delivery.get("device_id"),
                  delivery.get("controller_device_id")}
    return source_satellite in identities


def _validate_scope(source_satellite, context_source):
    for name, value in (("source_satellite", source_satellite), ("context_source", context_source)):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 160):
            raise JobError("invalid_format", f"{name} must be a non-empty string of at most 160 characters")


async def preview_timer_cancel(manager, *, source_satellite=None, context_source=None):
    _validate_scope(source_satellite, context_source)
    if source_satellite is not None and manager.alert_output:
        # Resolve exact entity/device/name/room identity through HA's registry.
        # This is registry-only and remains valid while the speaker is offline.
        requested_source = source_satellite
        try:
            source_satellite = (await manager.alert_output(source_satellite, "alarm"))["output"]
        except JobError as err:
            if err.code == "ambiguous_output":
                raise
            # Cancellation is HA-owned state control; an accepted timer stays
            # cancellable if its output was disabled or went offline. Retain
            # only the exact supplied identity for persisted-scope matching.
            if err.code not in {"unsupported_output", "unavailable", "invalid_output"}:
                raise
            source_satellite = requested_source
    async with manager._lock:
        jobs = [{"id": job["id"], "revision": job["revision"], "name": job.get("name"),
                 "duration": (job.get("payload") or {}).get("duration_seconds")}
                for job in manager.jobs.values()
                if job.get("kind") == "timer" and job.get("status") in ACTIVE_TIMER_STATUSES
                and _source_member(job, source_satellite, context_source)]
        if len(jobs) > 256:
            raise JobError("invalid_format", "too many active timers for one cancellation request")
        return {"selection": {"source_satellite": source_satellite,
                               "context_source": context_source, "jobs": jobs}}


def _validate_selection(selection):
    if not isinstance(selection, dict) or set(selection) != {"source_satellite", "context_source", "jobs"}:
        raise JobError("invalid_format", "selection requires source_satellite, context_source and jobs")
    _validate_scope(selection["source_satellite"], selection["context_source"])
    jobs = selection["jobs"]
    if not isinstance(jobs, list) or len(jobs) > 256:
        raise JobError("invalid_format", "selection jobs must be an array of at most 256 timers")
    seen = set()
    for item in jobs:
        if not isinstance(item, dict) or set(item) != {"id", "revision", "name", "duration"}:
            raise JobError("invalid_format", "each selected timer requires id, revision, name and duration")
        job_id, revision, name, duration = (item[key] for key in ("id", "revision", "name", "duration"))
        if not isinstance(job_id, str) or not job_id or len(job_id) > 160 or job_id in seen:
            raise JobError("invalid_format", "selected timer IDs must be unique non-empty strings")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise JobError("invalid_format", "selected timer revision must be a positive integer")
        if name is not None and (not isinstance(name, str) or len(name) > 160):
            raise JobError("invalid_format", "selected timer name must be a string or null")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            raise JobError("invalid_format", "selected timer duration must be a positive integer")
        seen.add(job_id)
    return jobs


async def cancel_timer_batch(manager, request_id, selection):
    items = _validate_selection(selection)
    if not isinstance(request_id, str) or not request_id or len(request_id) > 160:
        raise JobError("invalid_format", "request_id must be a non-empty string")
    async with manager._lock:
        replay = manager._replay(request_id)
        if replay is not None:
            if "timer_cancel_batch" not in replay:
                raise JobError("request_conflict", "request_id was used for another operation")
            return replay["timer_cancel_batch"]
        if not items:
            return {"status": "no_match", "receipts": [], "delivery_facts": {"stop_confirmed": False,
                                                                               "stop_unconfirmed": False}}

        original_jobs, original_requests = manager.jobs, manager.requests
        manager.jobs = copy.deepcopy(original_jobs)
        manager.requests = copy.deepcopy(original_requests)
        receipts = []
        changed_ids = []
        stop_required = []
        try:
            for item in items:
                job = manager.jobs.get(item["id"])
                if job is None:
                    receipts.append({"id": item["id"], "name": item["name"], "ok": False,
                                     "outcome": "not_found", "message": "Timer no longer exists."})
                    continue
                if job.get("kind") != "timer" or not _source_member(
                        job, selection["source_satellite"], selection["context_source"]):
                    receipts.append({"id": item["id"], "name": item["name"], "ok": False,
                                     "outcome": "foreign", "message": "Timer is outside the selected source."})
                    continue
                if job.get("revision") != item["revision"] or job.get("status") not in ACTIVE_TIMER_STATUSES:
                    receipts.append({"id": item["id"], "name": item["name"], "ok": False,
                                     "outcome": "stale", "message": "Timer changed since confirmation."})
                    continue
                delivery = job.get("delivery") or {}
                due = parse_utc(job["due_at"])
                was_presented_or_due = (job.get("status") in {"due", "sounding"}
                                        or (job.get("status") == "pending"
                                            and due <= manager.now().astimezone(timezone.utc))
                                        or delivery.get("transport_sent_at")
                                        or delivery.get("finish_attempted_at"))
                if job["status"] == "sounding":
                    job.update(status="dismissed", outcome="dismissed", revision=job["revision"] + 1)
                else:
                    job.update(status="cancelled", outcome="cancelled", revision=job["revision"] + 1)
                changed_ids.append(job["id"])
                result_job = copy.deepcopy(job)
                result_job["delivery_facts"] = delivery_facts(job)
                needs_stop = bool(was_presented_or_due and not result_job["delivery_facts"]["stop_confirmed"])
                stop_required.append(needs_stop)
                receipts.append({"id": job["id"], "name": job.get("name"), "ok": True,
                                 "outcome": "completed", "result": result_job,
                                 "delivery_facts": result_job["delivery_facts"],
                                 "stop_unconfirmed": needs_stop})

            succeeded = [item for item in receipts if item["ok"]]
            failed = [item for item in receipts if not item["ok"]]
            status = "partial" if succeeded and failed else "failed" if failed else "completed"
            confirmed = bool(receipts) and all(
                item.get("ok") and item.get("delivery_facts", {}).get("stop_confirmed") for item in receipts)
            result = {"status": status, "receipts": receipts,
                      "delivery_facts": {"stop_confirmed": confirmed,
                                         "stop_unconfirmed": any(stop_required)}}
            manager._remember(request_id, {"timer_cancel_batch": result})
            await manager._save()
        except BaseException:
            manager.jobs, manager.requests = original_jobs, original_requests
            raise
        for job_id in changed_ids:
            manager._cancel_schedule(job_id)
        return result
