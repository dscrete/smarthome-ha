"""Pure local-time recurrence rules for HA-owned alarms and reminders."""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class RecurrenceError(ValueError):
    """Invalid or unresolvable recurrence input."""


def normalize_schedule(raw, *, now: datetime, default_timezone: str) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str):
        raise RecurrenceError("schedule must be an object with kind")
    kind = raw["kind"]
    if kind == "relative":
        if set(raw) != {"kind", "seconds"}:
            raise RecurrenceError("relative schedule requires only seconds")
        seconds = raw["seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            raise RecurrenceError("relative seconds must be a positive integer")
        return {"kind": "once", "timezone": default_timezone,
                "due_at": _utc_iso(now + timedelta(seconds=seconds))}
    timezone_name = raw.get("timezone", default_timezone)
    if not isinstance(timezone_name, str) or not timezone_name:
        raise RecurrenceError("timezone must be an IANA timezone")
    zone = _zone(timezone_name)
    if kind == "once":
        allowed = {"kind", "date", "time", "timezone"}
        if set(raw) - allowed or "time" not in raw:
            raise RecurrenceError("once schedule requires time and optional date/timezone")
        local_time = _time(raw["time"])
        if "date" in raw:
            local_date = _date(raw["date"])
            due = _resolve_local(datetime.combine(local_date, local_time), zone)
        else:
            local_date = now.astimezone(zone).date()
            due = _resolve_local(datetime.combine(local_date, local_time), zone)
            if due <= now.astimezone(timezone.utc):
                due = _resolve_local(datetime.combine(local_date + timedelta(days=1), local_time), zone)
        return {"kind": "once", "timezone": timezone_name, "due_at": _utc_iso(due)}
    if kind in {"daily", "weekly"}:
        allowed = {"kind", "time", "timezone"} | ({"weekdays"} if kind == "weekly" else set())
        if set(raw) - allowed or "time" not in raw:
            raise RecurrenceError(f"{kind} schedule has unsupported or missing fields")
        local_time = _time(raw["time"])
        result = {"kind": kind, "time": local_time.isoformat(), "timezone": timezone_name}
        if kind == "weekly":
            weekdays = raw.get("weekdays")
            if not isinstance(weekdays, list) or not weekdays or any(
                    isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in weekdays):
                raise RecurrenceError("weekly weekdays must be a non-empty list of 0..6")
            if len(set(weekdays)) != len(weekdays):
                raise RecurrenceError("weekly weekdays must be unique")
            result["weekdays"] = sorted(weekdays)
        return result
    if kind == "monthly":
        if set(raw) - {"kind", "day", "time", "timezone"} or "day" not in raw or "time" not in raw:
            raise RecurrenceError("monthly schedule requires day and time")
        day = raw["day"]
        if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
            raise RecurrenceError("monthly day must be 1..31")
        return {"kind": kind, "day": day, "time": _time(raw["time"]).isoformat(), "timezone": timezone_name}
    if kind == "interval":
        if set(raw) - {"kind", "every", "unit", "anchor", "timezone"}:
            raise RecurrenceError("interval schedule has unsupported fields")
        every, unit = raw.get("every"), raw.get("unit")
        if isinstance(every, bool) or not isinstance(every, int) or every <= 0:
            raise RecurrenceError("interval every must be a positive integer")
        if unit not in {"minutes", "hours", "days", "weeks"}:
            raise RecurrenceError("interval unit must be minutes, hours, days or weeks")
        anchor = raw.get("anchor")
        if anchor is None:
            anchor_local = now.astimezone(zone).replace(tzinfo=None)
        elif isinstance(anchor, str):
            try:
                anchor_local = datetime.fromisoformat(anchor)
            except ValueError as error:
                raise RecurrenceError("interval anchor must be local YYYY-MM-DDTHH:MM[:SS]") from error
            if anchor_local.tzinfo is not None:
                raise RecurrenceError("interval anchor must be local to its schedule timezone")
        else:
            raise RecurrenceError("interval anchor must be a local datetime string")
        return {"kind": kind, "every": every, "unit": unit,
                "anchor": anchor_local.isoformat(timespec="seconds"), "timezone": timezone_name}
    raise RecurrenceError("unsupported schedule kind")


def next_occurrence(schedule: dict, after: datetime) -> datetime:
    """Return the first occurrence strictly after the UTC instant ``after``."""
    after = after.astimezone(timezone.utc)
    kind = schedule["kind"]
    if kind == "once":
        due = datetime.fromisoformat(schedule["due_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        return due if due > after else None
    zone = _zone(schedule["timezone"])
    local_after = after.astimezone(zone)
    if kind == "daily":
        local_time = _time(schedule["time"])
        for offset in range(0, 370):
            candidate = _resolve_local(datetime.combine(local_after.date() + timedelta(days=offset), local_time), zone)
            if candidate > after:
                return candidate
    elif kind == "weekly":
        local_time = _time(schedule["time"])
        weekdays = set(schedule["weekdays"])
        for offset in range(0, 15):
            current_date = local_after.date() + timedelta(days=offset)
            if current_date.weekday() not in weekdays:
                continue
            candidate = _resolve_local(datetime.combine(current_date, local_time), zone)
            if candidate > after:
                return candidate
    elif kind == "monthly":
        local_time = _time(schedule["time"])
        year, month = local_after.year, local_after.month
        for offset in range(0, 240):
            index = year * 12 + month - 1 + offset
            candidate_year, candidate_month = index // 12, index % 12 + 1
            if schedule["day"] > calendar.monthrange(candidate_year, candidate_month)[1]:
                continue
            candidate = _resolve_local(datetime.combine(date(candidate_year, candidate_month, schedule["day"]), local_time), zone)
            if candidate > after:
                return candidate
    elif kind == "interval":
        anchor = datetime.fromisoformat(schedule["anchor"])
        every, unit = schedule["every"], schedule["unit"]
        if unit in {"minutes", "hours"}:
            seconds = every * (60 if unit == "minutes" else 3600)
            anchor_utc = _resolve_local(anchor, zone)
            steps = max(0, int((after - anchor_utc).total_seconds() // seconds) + 1)
            candidate = anchor_utc + timedelta(seconds=steps * seconds)
            return candidate if candidate > after else candidate + timedelta(seconds=seconds)
        days = every * (7 if unit == "weeks" else 1)
        # Resolve each occurrence independently. A DST gap may shift this one
        # occurrence, but must not move the schedule's saved wall-clock anchor.
        local_anchor = anchor
        local_now = local_after.replace(tzinfo=None)
        steps = max(0, (local_now.date() - local_anchor.date()).days // days)
        for _ in range(3):
            candidate = _resolve_local(local_anchor + timedelta(days=steps * days), zone)
            if candidate > after:
                return candidate
            steps += 1
    raise RecurrenceError("could not find a future occurrence within the supported horizon")


def latest_occurrence(schedule: dict, first_due: datetime, through: datetime, *, limit: int = 512) -> datetime:
    """Find latest occurrence without iterating through long downtime intervals."""
    latest = first_due.astimezone(timezone.utc)
    through = through.astimezone(timezone.utc)
    if latest > through:
        return None
    kind = schedule["kind"]
    if kind == "once":
        return latest
    zone = _zone(schedule["timezone"])
    if kind == "interval":
        every, unit = schedule["every"], schedule["unit"]
        if unit in {"minutes", "hours"}:
            seconds = every * (60 if unit == "minutes" else 3600)
            anchor = _resolve_local(datetime.fromisoformat(schedule["anchor"]), zone)
            steps = (int((through - anchor).total_seconds() // seconds))
            due = anchor + timedelta(seconds=steps * seconds)
            while due < latest:
                due += timedelta(seconds=seconds)
            return due if due <= through else due - timedelta(seconds=seconds)
        period = every * (7 if unit == "weeks" else 1)
        local_anchor = datetime.fromisoformat(schedule["anchor"])
        local_through = through.astimezone(zone).replace(tzinfo=None)
        steps = max(0, (local_through.date() - local_anchor.date()).days // period)
        due = _resolve_local(local_anchor + timedelta(days=steps * period), zone)
        while due > through:
            steps -= 1
            due = _resolve_local(local_anchor + timedelta(days=max(0, steps) * period), zone)
        while True:
            following = _resolve_local(local_anchor + timedelta(days=(steps + 1) * period), zone)
            if following > through:
                break
            steps += 1
            due = following
        return due if due >= latest else None
    local_through = through.astimezone(zone)
    if kind == "daily":
        candidate = _resolve_local(datetime.combine(local_through.date(), _time(schedule["time"])), zone)
        if candidate > through:
            candidate = _resolve_local(datetime.combine(local_through.date() - timedelta(days=1), _time(schedule["time"])), zone)
    elif kind == "weekly":
        candidate = None
        for offset in range(15):
            day = local_through.date() - timedelta(days=offset)
            if day.weekday() in schedule["weekdays"]:
                possible = _resolve_local(datetime.combine(day, _time(schedule["time"])), zone)
                if possible <= through:
                    candidate = possible
                    break
    elif kind == "monthly":
        candidate = None
        for offset in range(12):
            index = local_through.year * 12 + local_through.month - 1 - offset
            year, month = index // 12, index % 12 + 1
            day_number = schedule["day"]
            if day_number > calendar.monthrange(year, month)[1]:
                continue
            possible = _resolve_local(datetime.combine(date(year, month, day_number), _time(schedule["time"])), zone)
            if possible <= through:
                candidate = possible
                break
    else:
        raise RecurrenceError("unsupported schedule kind")
    return candidate if candidate is not None and candidate >= latest else None


def _resolve_local(local: datetime, zone: ZoneInfo) -> datetime:
    """Choose fold 0 and move nonexistent wall times to the first valid minute."""
    original = local
    for _ in range(48 * 60):
        candidate = local.replace(tzinfo=zone, fold=0)
        roundtrip = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if roundtrip == local:
            return candidate.astimezone(timezone.utc)
        if local == original:
            local = local.replace(second=0, microsecond=0)
        local += timedelta(minutes=1)
    raise RecurrenceError("local time is invalid in the selected timezone")


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise RecurrenceError("timezone must be a valid IANA timezone") from error


def _time(value):
    if not isinstance(value, str):
        raise RecurrenceError("time must be HH:MM[:SS]")
    try:
        result = time.fromisoformat(value)
    except ValueError as error:
        raise RecurrenceError("time must be HH:MM[:SS]") from error
    if result.tzinfo is not None:
        raise RecurrenceError("time must be local and must not include an offset")
    return result


def _date(value):
    if not isinstance(value, str):
        raise RecurrenceError("date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise RecurrenceError("date must be YYYY-MM-DD") from error


def _utc_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
