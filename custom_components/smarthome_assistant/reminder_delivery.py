"""One-shot, durably reserved Assist announcements for due reminders."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .jobs import JobError, parse_utc

_LOGGER = logging.getLogger(__name__)


class ReminderDelivery:
    """Deliver reminders without replaying an announcement of uncertain outcome."""

    def __init__(self, manager, output):
        self.manager, self.output = manager, output
        self._wake = asyncio.Event()
        self._task = None
        self._closed = False
        self._output_locks = {}
        self._remove_listener = manager.add_change_listener(self.wake)

    def wake(self):
        self._wake.set()

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="smarthome-reminders")

    async def stop(self):
        self._closed = True
        self._remove_listener()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def deliver_due(self):
        jobs = await self.manager.async_list(kinds=["reminder"], statuses=["due"])
        await asyncio.gather(*(self._deliver(candidate) for candidate in jobs), return_exceptions=True)

    async def _deliver(self, candidate):
        delivery = candidate.get("delivery", {})
        output = delivery.get("output")
        if not output:
            return
        lock = self._output_locks.setdefault(output, asyncio.Lock())
        async with lock:
            if candidate.get("delivery", {}).get("alert_attempted_at"):
                return
            try:
                description = self.output.describe_output(output)
                if (not description["available"] or not description["supports_announce"]
                        or description.get("device_id") != delivery.get("device_id")):
                    return
                state = self.manager.hass.states.get(output)
                if not state or state.state not in {"idle", "ready"}:
                    return
                if delivery.get("mac_address") and self.output_mac(output) != delivery["mac_address"]:
                    return
                reserved = await self.manager.async_reminder_reserve(candidate["id"], candidate["revision"])
                if reserved is None:
                    return
                message = self._message(reserved)
                try:
                    async with asyncio.timeout(10):
                        await self.output.announce_message(output, message)
                except Exception as error:
                    _LOGGER.warning("Reminder announcement outcome is uncertain for %s", candidate["id"],
                                    exc_info=True)
                    await self.manager.async_reminder_finish(reserved["id"], reserved["revision"], error=error)
                else:
                    await self.manager.async_reminder_finish(reserved["id"], reserved["revision"])
            except JobError:
                # Before reservation, missing/disconnected outputs are safe to retry.
                return

    def output_mac(self, output):
        """Read the currently registered identity through the configured output adapter."""
        getter = getattr(self.output, "target_mac", None)
        return getter(output) if getter else None

    def _message(self, job):
        message = job["payload"]["message"]
        original_due = job.get("original_due_at", job["due_at"])
        if (self.manager.now().astimezone(parse_utc(original_due).tzinfo) - parse_utc(original_due)).total_seconds() < 60:
            return message
        schedule = self.manager.schedules.get(job.get("schedule_id"), {})
        zone_name = schedule.get("schedule", {}).get("timezone", "UTC")
        try:
            local = datetime.fromisoformat(original_due.replace("Z", "+00:00")).astimezone(ZoneInfo(zone_name))
            return f"{message}. This reminder was scheduled for {local.strftime('%Y-%m-%d %H:%M')} {zone_name}."
        except (ValueError, TypeError, KeyError):
            return f"{message}. This reminder was originally scheduled for {original_due}."

    async def _run(self):
        while not self._closed:
            self._wake.clear()
            try:
                await self.deliver_due()
            except Exception:
                _LOGGER.exception("Reminder reconciliation failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except TimeoutError:
                pass
