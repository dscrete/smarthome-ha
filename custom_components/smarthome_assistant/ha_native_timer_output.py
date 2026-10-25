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
from homeassistant.components.assist_satellite import AssistSatelliteEntityFeature
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

    def describe_outputs(self) -> list[dict[str, Any]]:
        """Describe registered Assist satellites without requiring them online."""
        registry = er.async_get(self.hass)
        return [self.describe_output(entry.entity_id) for entry in registry.entities.values()
                if entry.entity_id.startswith("assist_satellite.")]

    def resolve_alert_output(self, source: str, kind: str, *, outputs=None) -> dict[str, Any]:
        """Resolve only exact registered output, name/alias, or room identity."""
        if not isinstance(source, str) or not source.strip():
            raise JobError("unsupported_output", "No alert source or default Assist satellite is configured")
        requested = source.strip().casefold()
        outputs = self.describe_outputs() if outputs is None else outputs
        capable = ([item for item in outputs if item["supports_timer"]] if kind == "alarm" else
                   [item for item in outputs if item["supports_announce"]])
        # The exact entity/device and exact name/alias selectors outrank rooms.
        direct = [item for item in capable if item["entity_id"].casefold() == requested
                  or (item.get("device_id") or "").casefold() == requested]
        if not direct:
            registry = er.async_get(self.hass)
            for item in capable:
                entry = registry.async_get(item["entity_id"])
                aliases = getattr(entry, "aliases", ()) if entry else ()
                names = {item["name"].casefold(), *(str(alias).casefold() for alias in aliases)}
                if requested in names:
                    direct.append(item)
        candidates = direct
        if not candidates:
            candidates = [item for item in capable if requested in {
                (item.get("area_id") or "").casefold(), (item.get("area_name") or "").casefold()}]
        if not candidates:
            raise JobError("unsupported_output", "No registered alert output exactly matches that output or room")
        if len(candidates) != 1:
            error = JobError("ambiguous_output", "More than one registered alert output matches that name or room")
            error.choices = candidates
            raise error
        return candidates[0]

    def describe_output(self, output: str) -> dict[str, Any]:
        entry = self._entity_entry(output)
        if entry is None or not output.startswith("assist_satellite."):
            raise JobError("unsupported_output", "Output must be a registered Assist satellite")
        device = dr.async_get(self.hass).async_get(entry.device_id) if entry.device_id else None
        state = self.hass.states.get(output)
        available = bool(state and state.state not in {"unknown", "unavailable"}
                         and entry.disabled_by is None and device is not None
                         and getattr(device, "disabled_by", None) is None)
        supports_timer = False
        if entry.device_id:
            try:
                manager = self.hass.data.get(TIMER_DATA)
                handlers = getattr(manager, "handlers", None)
                supports_timer = (async_device_supports_timers(self.hass, entry.device_id)
                                  and type(handlers) is dict and callable(handlers.get(entry.device_id)))
            except Exception:
                supports_timer = False
        supports_timer = supports_timer or self._declares_timer_support(entry)
        area_id = getattr(entry, "area_id", None) or getattr(device, "area_id", None)
        try:
            from homeassistant.helpers import area_registry as ar
            area = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
        except Exception:
            area = None
        name = (getattr(state, "attributes", {}).get("friendly_name") if state else None) or getattr(entry, "name", None) or output
        features = getattr(state, "attributes", {}).get("supported_features", 0) if state else 0
        features = features if isinstance(features, int) and not isinstance(features, bool) else 0
        return {"entity_id": output, "name": name, "area_id": area_id,
                "area_name": getattr(area, "name", None), "available": available,
                "supports_timer": bool(supports_timer and entry.disabled_by is None and device is not None
                    and getattr(device, "disabled_by", None) is None),
                "supports_announce": bool(features & AssistSatelliteEntityFeature.ANNOUNCE
                    and entry.disabled_by is None and device is not None
                    and getattr(device, "disabled_by", None) is None),
                "device_id": entry.device_id}

    def _declares_timer_support(self, entry) -> bool:
        """Use ESPHome's restored device metadata while its handler is offline.

        HA 2026.10 restores device_info and api_version on the ESPHome config
        entry. This is capability evidence only: snapshot() still requires a
        live registered handler before any native event or new timer is sent.
        """
        if entry.platform != "esphome":
            return False
        config_entries = getattr(self.hass, "config_entries", None)
        if config_entries is None:
            return False
        config_entry = config_entries.async_get_entry(getattr(entry, "config_entry_id", None))
        runtime = getattr(config_entry, "runtime_data", None)
        info = getattr(runtime, "device_info", None)
        api_version = getattr(runtime, "api_version", None)
        if info is None or api_version is None:
            return False
        try:
            from aioesphomeapi.model import VoiceAssistantFeature
            return bool(info.voice_assistant_feature_flags_compat(api_version) & VoiceAssistantFeature.TIMERS)
        except (AttributeError, TypeError, ImportError, ValueError):
            return False

    def target_mac(self, output: str) -> str | None:
        """Return the unique registered MAC identity, when HA knows one."""
        entry = self._entity_entry(output)
        device = dr.async_get(self.hass).async_get(entry.device_id) if entry and entry.device_id else None
        if device is None:
            return None
        values = {str(value).replace(":", "").replace("-", "").lower()
                  for kind, value in device.connections if kind == "mac"}
        values.update(str(value).replace(":", "").replace("-", "").lower()
                      for kind, value in device.identifiers if kind == "esphome")
        values = {value for value in values if len(value) == 12 and all(c in "0123456789abcdef" for c in value)}
        return next(iter(values)) if len(values) == 1 else None

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
            if job.get("status") == "paused":
                remaining = job.get("paused_remaining_seconds")
                if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining <= 0:
                    raise ValueError("paused timer requires positive frozen remaining seconds")
            else:
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
                is_active=event not in {"cancelled", "finished"} and job.get("status") != "paused",
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
        await self.announce_message(output, f"{name} timer finished." if name else "Timer finished.")

    async def announce_message(self, output: str, message: str) -> None:
        """Speak caller-provided reminder text through the registered satellite."""
        self.describe_output(output)
        state = self.hass.states.get(output)
        if state is None or state.state in {"unknown", "unavailable"}:
            raise JobError("unavailable", "Assist satellite is disconnected")
        if not isinstance(message, str) or not message.strip():
            raise JobError("invalid_format", "Announcement message must be non-empty")
        await self.hass.services.async_call(
            "assist_satellite",
            "announce",
            {
                "entity_id": output,
                "message": message,
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
