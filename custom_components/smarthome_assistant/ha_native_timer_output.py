"""Deliver durable jobs through Home Assistant's native ESPHome timer path.

Timer handler lookup is an isolated compatibility boundary. HA 2026.10 exposes
``async_device_supports_timers`` publicly, while its timer manager keeps the
registered handlers in ``hass.data[TIMER_DATA].handlers`` without a public
dispatch method for externally owned jobs. This adapter validates that
internal shape at runtime and fails closed if it changes. It never replaces or
monkeypatches registered handlers.
"""
from __future__ import annotations

from datetime import datetime, timezone
import inspect
import math
import time
from typing import Any

from homeassistant.components.intent import (
    TimerEventType,
    TimerInfo,
    async_device_supports_timers,
)
from homeassistant.components.intent.const import TIMER_DATA
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .jobs import JobError


_EVENT_TYPES = {
    "started": TimerEventType.STARTED,
    "updated": TimerEventType.UPDATED,
    "cancelled": TimerEventType.CANCELLED,
    "finished": TimerEventType.FINISHED,
}


class HAStandardTimerOutput:
    """Resolve and invoke a registered HA native Assist satellite timer handler."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    async def available(self, source: str) -> dict[str, Any]:
        """Resolve one enabled ESPHome Assist satellite and verify its handler."""
        output = self._resolve(source)
        device_id, _handler = self.snapshot(output)
        return {
            "output": output,
            "delivery_owner": "ha",
            "protocol_version": 1,
            "delivery_protocol": "assist_native",
            "device_id": device_id,
        }

    def snapshot(self, output: str) -> tuple[str, Any]:
        """Return the live device ID and current HA-registered timer callback."""
        entry = self._entity_entry(output)
        if entry is None or entry.platform != "esphome" or not output.startswith("assist_satellite."):
            raise JobError("unsupported_output", "Timer output must be a registered ESPHome Assist satellite")
        if entry.disabled_by is not None:
            raise JobError("unavailable", "The Assist satellite is disabled")
        if not entry.device_id:
            raise JobError("unavailable", "The Assist satellite has no registered HA device")

        device = dr.async_get(self.hass).async_get(entry.device_id)
        if device is None or getattr(device, "disabled_by", None) is not None:
            raise JobError("unavailable", "The Assist satellite device is missing or disabled")
        state = self.hass.states.get(output)
        if state is None or state.state in {"unknown", "unavailable"}:
            raise JobError("unavailable", "The Assist satellite is disconnected")

        try:
            supports_timers = async_device_supports_timers(self.hass, entry.device_id)
            manager = self.hass.data.get(TIMER_DATA)
            handlers = getattr(manager, "handlers", None)
            # HA 2026.10 stores one handler per device in a plain dict. Keep
            # this private lookup local and reject an incompatible data shape.
            if not supports_timers or type(handlers) is not dict:
                raise JobError("unsupported_output", "The ESPHome satellite has no registered timer handler")
            handler = handlers.get(entry.device_id)
            if not callable(handler):
                raise JobError("unsupported_output", "The ESPHome satellite timer handler is not registered")
            if inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
                getattr(handler, "__call__", None)
            ):
                raise JobError("unavailable", "The ESPHome satellite timer handler is asynchronous")
        except JobError:
            raise
        except Exception as error:
            raise JobError("unavailable", "Home Assistant timer handler API is unavailable") from error
        return entry.device_id, handler

    def send(self, output: str, event: str, job: dict[str, Any]) -> None:
        """Send one standard timer event to the currently registered handler."""
        try:
            event_type = _EVENT_TYPES[event]
        except (KeyError, TypeError) as error:
            raise JobError("invalid_format", "Unsupported native timer event") from error
        device_id, handler = self.snapshot(output)
        try:
            delivery = job.get("delivery")
            bound_device_id = delivery.get("device_id") if isinstance(delivery, dict) else None
            if bound_device_id is not None and bound_device_id != device_id:
                raise JobError("invalid_output", "The timer job is bound to a different HA device")
            duration = job["payload"]["duration_seconds"]
            if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
                raise ValueError("duration_seconds must be a positive integer")
            due_at = _parse_due_at(job["due_at"])
            remaining = max(1, math.ceil((due_at - datetime.now(timezone.utc)).total_seconds()))
            monotonic_now = time.monotonic_ns()
            timer = TimerInfo(
                id=job["id"],
                name=job.get("name"),
                # HA's TimerInfo captures created_seconds in __post_init__.
                # Construct with the durable original duration, then set its
                # live countdown to the ceiling of due_at minus wall time.
                seconds=duration,
                device_id=device_id,
                start_hours=duration // 3600,
                start_minutes=(duration % 3600) // 60,
                start_seconds=duration % 60,
                created_at=monotonic_now,
                updated_at=monotonic_now,
                language="en",
                is_active=event not in {"cancelled", "finished"},
            )
            timer.seconds = remaining if event in {"started", "updated"} else 0
            timer.updated_at = monotonic_now
        except JobError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise JobError("invalid_format", "Native timer job data is invalid") from error
        handler(event_type, timer)

    async def announce(self, output: str, name: str | None) -> None:
        """Ask HA's standard Assist Satellite service to announce the timer."""
        self.snapshot(output)
        await self.hass.services.async_call(
            "assist_satellite",
            "announce",
            {
                "entity_id": output,
                "message": f"{name} timer finished.",
                "preannounce": False,
            },
            blocking=True,
        )

    def _resolve(self, source: str) -> str:
        if not isinstance(source, str) or not source:
            raise JobError("unsupported_output", "Timer source must identify an Assist satellite or HA device")
        registry = er.async_get(self.hass)
        if source.startswith("assist_satellite."):
            entry = registry.async_get(source)
            if entry is None:
                raise JobError("unsupported_output", "Assist satellite is not registered")
            return source

        matches = [
            entry.entity_id
            for entry in registry.entities.values()
            if entry.device_id == source
            and entry.entity_id.startswith("assist_satellite.")
            and self._enabled_available_entry(entry)
        ]
        if not matches:
            raise JobError("unsupported_output", "HA device does not resolve to one Assist satellite")
        if len(matches) > 1:
            raise JobError("unavailable", "HA device resolves to multiple available Assist satellites")
        return matches[0]

    def _entity_entry(self, output: str):
        return er.async_get(self.hass).async_get(output)

    def _enabled_available_entry(self, entry: Any) -> bool:
        if entry.platform != "esphome" or entry.disabled_by is not None or not entry.device_id:
            return False
        device = dr.async_get(self.hass).async_get(entry.device_id)
        state = self.hass.states.get(entry.entity_id)
        return (device is not None and getattr(device, "disabled_by", None) is None
                and state is not None and state.state not in {"unknown", "unavailable"})


def _parse_due_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        due_at = value
    elif isinstance(value, str):
        due_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("due_at must be an RFC3339 timestamp")
    if due_at.tzinfo is None:
        raise ValueError("due_at must include a timezone")
    return due_at.astimezone(timezone.utc)
