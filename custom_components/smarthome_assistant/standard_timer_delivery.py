"""Portable timer projections over HA's existing ESPHome connection.

HA jobs own the countdown. Satellites receive ordinary timer events, not another
Assist countdown. FINISHED is reserved durably and sent at most once because the
standard protocol has no durable dismissal receipt. A successful send means
submission only; it does not establish playback or local dismissal.
"""
from __future__ import annotations

import asyncio
import logging

from .jobs import JobError, NATIVE_PROTOCOL, TIMER_GRACE, parse_utc

_LOGGER = logging.getLogger(__name__)


class StandardTimerDelivery:
    def __init__(self, manager, output):
        self.manager, self.output = manager, output
        self._sent = {}
        self._stopped = set()
        self._wake = asyncio.Event()
        self._task = None
        self._closed = False

    def wake(self):
        self._wake.set()

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="smarthome-standard-timers")

    async def stop(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def available(self, source):
        if self._closed:
            raise JobError("unavailable", "HA timer delivery is stopping")
        return await self.output.available(source)

    def _identity(self, job):
        output = job["delivery"]["output"]
        device, handler = self.output.snapshot(output)
        if device != job["delivery"]["device_id"]:
            raise JobError("invalid_output", "The accepted timer belongs to a different HA device")
        return output, (device, id(getattr(handler, "__self__", handler)))

    async def sync(self):
        jobs = [job for job in await self.manager.async_list(kinds=["timer", "alarm"])
                if job.get("delivery", {}).get("protocol") == NATIVE_PROTOCOL]
        now = self.manager.now()
        outstanding = False
        for job in jobs:
            cutoff = parse_utc(job["due_at"]) + TIMER_GRACE
            stop = (job["status"] in {"cancelled", "dismissed", "expired", "missed", "unknown"}
                    or (job["status"] != "paused" and now >= cutoff))
            if not stop and job["status"] == "completed":
                outstanding = True  # Wait for the absolute cutoff, not another FINISHED.
                continue
            try:
                output, connection = self._identity(job)
                key = (job["id"], job["delivery"]["token"], connection)
                if stop:
                    if key not in self._stopped:
                        self.output.send(output, "cancelled", job)
                        self._stopped.add(key)
                    continue
                outstanding = True
                if job["status"] == "pending":
                    if self._sent.get(key) != job["revision"]:
                        event = "updated" if key in self._sent else "started"
                        self.output.send(output, event, job)
                        self._sent[key] = job["revision"]
                elif job["status"] == "paused":
                    if self._sent.get(key) != job["revision"]:
                        self.output.send(output, "updated", job)
                        self._sent[key] = job["revision"]
                elif job["status"] == "due":
                    reserved = await self.manager.async_native_begin(job["id"], job["revision"])
                    if reserved is None:
                        continue
                    name_status = "absent"
                    if reserved.get("name"):
                        try:
                            async with asyncio.timeout(min(3, max(.001, (cutoff - self.manager.now()).total_seconds()))):
                                if reserved["kind"] == "alarm":
                                    await self.output.announce_message(output, reserved.get("name") or "Alarm.")
                                else:
                                    await self.output.announce(output, reserved.get("name"))
                            name_status = "announced"
                        except Exception:
                            name_status = "unknown"
                            _LOGGER.warning("Standard timer name delivery is unavailable or uncertain for %s", job["id"])
                    await self.manager.async_native_finish(reserved["id"], reserved["revision"],
                        lambda current: self.output.send(output, "finished", current), name_status=name_status)
            except (JobError, ConnectionError, TimeoutError):
                # Unsent due jobs can wait within their original grace. A saved
                # FINISHED reservation can never be replayed after an exception.
                outstanding = outstanding or (job["status"] in {"pending", "due"})
                self._sent = {key: value for key, value in self._sent.items() if key[0] != job["id"]}
                self._stopped = {key for key in self._stopped if key[0] != job["id"]}
                continue
        live = {job["id"] for job in jobs}
        self._sent = {key: value for key, value in self._sent.items() if key[0] in live}
        self._stopped = {key for key in self._stopped if key[0] in live}
        return outstanding

    async def _run(self):
        while not self._closed:
            self._wake.clear()
            try:
                outstanding = await self.sync()
            except Exception:
                _LOGGER.exception("Standard timer reconciliation unavailable")
                outstanding = True
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2 if outstanding else 30)
            except TimeoutError:
                pass
