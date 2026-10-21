"""HA-owned projection of durable jobs onto bound native EchoMuse outputs.

Only a configured ESPHome exchange action is called, never a stored job payload.
The action returns durable presentation receipts, not permission to execute home
actions. Core and Gateway do not participate in this worker.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
from uuid import uuid4

from .jobs import JobError, NATIVE_PROTOCOL, TIMER_GRACE, iso_utc, parse_utc
from .const import validate_timer_outputs

_LOGGER = logging.getLogger(__name__)
ACTION_SUFFIX = "_smarthome_timer_exchange_v1"
MAX_EXCHANGE_BYTES = 65536


class TimerDelivery:
    """Serialize each output's complete projection and acknowledge saved receipts."""

    def __init__(self, manager, bindings, *, exchange, identity, audio=None, target_mac=None):
        self.manager = manager
        self.bindings = dict(bindings)
        self.exchange = exchange
        self.identity = identity
        self.audio = audio
        self.target_mac = target_mac
        self._locks = {key: asyncio.Lock() for key in self.bindings}
        self._acks = {key: [] for key in self.bindings}
        self._audio = {}
        self._wake = asyncio.Event()
        self._task = None
        self._closed = False

    def wake(self):
        self._wake.set()

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="smarthome-native-timers")

    async def stop(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def available(self, source):
        if self._closed:
            raise JobError("unavailable", "HA timer delivery is stopping")
        output = next((key for key in self.bindings if self.identity(key, None, source)), None)
        if output is None:
            raise JobError("unsupported_output", "No HA-owned native timer output is bound to this satellite")
        try:
            response = await self.sync(output)
            if response["presentation"].get("state") not in {"idle", "ready", "sounding"}:
                raise JobError("unavailable", "The timer output is disconnected or cannot play alerts")
        except JobError:
            raise
        except Exception as error:
            _LOGGER.warning("Native timer output verification failed for %s", output, exc_info=True)
            raise JobError("unavailable", "The native timer output could not be verified") from error
        return {"output": output, "delivery_owner": "ha", "protocol_version": 1,
                **({"mac_address": response["mac_address"], "controller_device_id": response["device_id"]}
                   if self.target_mac else {})}

    async def sync(self, output):
        async with self._locks[output]:
            expected_mac = self.target_mac(output) if self.target_mac else None
            if self.target_mac and not expected_mac:
                raise JobError("invalid_output", "The satellite has no unique registered native MAC identity")
            jobs = await self.manager.async_list(kinds=["timer"], statuses=["pending", "due", "sounding"])
            jobs = [job for job in jobs if job.get("delivery", {}).get("owner") == "ha"
                    and job["delivery"].get("protocol") != NATIVE_PROTOCOL
                    and job["delivery"].get("output") == output]
            if expected_mac and any(job["delivery"].get("mac_address") != expected_mac for job in jobs):
                raise JobError("invalid_output", "An accepted timer belongs to a different output identity")
            async def prepare_audio(job):
                if job.get("name") and self.audio:
                    cache_key = (job["id"], job["delivery"]["token"], job["name"])
                    if cache_key not in self._audio:
                        try:
                            async with asyncio.timeout(3):
                                self._audio[cache_key] = await self.audio(job)
                        except Exception as error:
                            _LOGGER.warning("Timer name audio unavailable for %s: %s", job["id"], type(error).__name__)
                            self._audio[cache_key] = None
            await asyncio.gather(*(prepare_audio(job) for job in jobs))
            # Names may need fresh TTS. Reload after that await so cancellation,
            # edits and terminal state are not projected from an old snapshot.
            all_jobs = await self.manager.async_list(kinds=["timer"], statuses=["pending", "due", "sounding"])
            jobs = [job for job in all_jobs if job.get("delivery", {}).get("owner") == "ha"
                    and job["delivery"].get("protocol") != NATIVE_PROTOCOL
                    and job["delivery"].get("output") == output]
            projected = []
            for job in jobs:
                cache_key = (job["id"], job["delivery"]["token"], job.get("name"))
                audio_url = self._audio.get(cache_key)
                projected.append({"job_id": job["id"], "delivery_token": job["delivery"]["token"],
                    "revision": job["revision"], "due_at": job["due_at"],
                    "cutoff_at": iso_utc(parse_utc(job["due_at"]) + TIMER_GRACE),
                    "status": job["status"], "name": job.get("name"), "audio_url": audio_url})
            request = {"version": 1, "exchange_id": uuid4().hex,
                       "authority_id": self.manager.authority_id,
                       "sequence": self.manager.delivery_sequence(), "now": iso_utc(self.manager.now()),
                       "timers": projected, "ack_receipts": self._acks[output]}
            if expected_mac:
                request["target_mac_address"] = ":".join(expected_mac[i:i+2] for i in range(0, 12, 2))
            payload = json.dumps(request, separators=(",", ":"), allow_nan=False)
            if len(payload.encode("utf-8")) > MAX_EXCHANGE_BYTES:
                raise JobError("unavailable", "Complete timer projection exceeds native output capacity")
            response = await self.exchange(self.bindings[output], payload)
            if (not isinstance(response, dict) or type(response.get("version")) is not int or response.get("version") != 1
                    or response.get("exchange_id") != request["exchange_id"]
                    or response.get("error") or not isinstance(response.get("presentation"), dict)
                    or not isinstance(response.get("receipts"), list)
                    or not self.identity(output, response, output)):
                raise JobError("invalid_output", "Native timer response identity or contract does not match")
            try:
                response_size = len(json.dumps(response, allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError) as error:
                raise JobError("invalid_output", "Native timer response is not valid JSON") from error
            if response_size > MAX_EXCHANGE_BYTES:
                raise JobError("invalid_output", "Native timer response exceeds capacity")
            # Never acknowledge an outbox item before its state/evidence is saved.
            self._acks[output] = await self.manager.async_delivery_receipts(output, response["receipts"])
            live = {(job["id"], job["delivery"]["token"], job.get("name")) for job in all_jobs
                    if job.get("delivery", {}).get("owner") == "ha"}
            self._audio = {key: value for key, value in self._audio.items() if key in live}
            return response

    async def _run(self):
        while not self._closed:
            self._wake.clear()
            results = await asyncio.gather(*(self.sync(key) for key in self.bindings), return_exceptions=True)
            for key, result in zip(self.bindings, results):
                if isinstance(result, Exception):
                    _LOGGER.debug("Native timer reconciliation unavailable for %s: %s", key, result)
            jobs = await self.manager.async_list(kinds=["timer"], statuses=["pending", "due", "sounding"])
            outstanding = any(job.get("delivery", {}).get("owner") == "ha"
                              and job["delivery"].get("protocol") != NATIVE_PROTOCOL for job in jobs) or any(self._acks.values())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2 if outstanding else 30)
            except TimeoutError:
                pass


def configure_exchange_delivery(hass, manager, bindings):
    """Wire public HA services, registries and TTS helpers into the worker."""
    bindings = validate_timer_outputs(bindings)
    if bindings == {}:
        async def unavailable(*_args):
            raise JobError("unsupported_output", "No HA-owned native timer outputs are configured")
        delivery = TimerDelivery(manager, {}, exchange=unavailable, identity=lambda *_args: False)
        manager.timer_output = delivery.available
        manager.on_change = delivery.wake
        return delivery
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from homeassistant.components import assist_pipeline, media_source, tts
    from homeassistant.components.media_player import async_process_play_media_url

    def registered_device(output):
        entity = er.async_get(hass).async_get(output)
        if entity is None or not entity.device_id:
            return None
        return dr.async_get(hass).async_get(entity.device_id)

    def mac(value):
        return str(value).replace(":", "").replace("-", "").lower()

    def target_mac(output):
        device = registered_device(output)
        if device is None:
            return None
        addresses = {mac(value) for kind, value in device.connections if kind == "mac"}
        addresses.update(mac(value) for kind, value in device.identifiers if kind == "esphome")
        addresses = {value for value in addresses if len(value) == 12 and all(c in "0123456789abcdef" for c in value)}
        return next(iter(addresses)) if len(addresses) == 1 else None

    def identity(output, response, source):
        device = registered_device(output)
        if device is None or source not in {output, device.id}:
            return False
        if response is None:
            return True
        return mac(response.get("mac_address", "")) == target_mac(output) and bool(response.get("device_id"))

    async def exchange(action, payload):
        domain, service = action.split(".", 1)
        async with asyncio.timeout(8):
            return await hass.services.async_call(domain, service, {"payload": payload},
                                                  blocking=True, return_response=True)

    async def audio(job):
        pipeline = assist_pipeline.async_get_pipeline(hass)
        if not pipeline.tts_engine:
            raise JobError("unavailable", "The preferred HA Assist pipeline has no TTS engine")
        media_id = tts.generate_media_source_id(hass, f"{job['name']} timer finished.",
            engine=pipeline.tts_engine, language=pipeline.tts_language, cache=True)
        media = await media_source.async_resolve_media(hass, media_id, target_media_player=None)
        return async_process_play_media_url(hass, media.url)

    delivery = TimerDelivery(manager, bindings, exchange=exchange, identity=identity, audio=audio, target_mac=target_mac)
    manager.timer_output = delivery.available
    manager.on_change = delivery.wake
    return delivery


class CombinedTimerDelivery:
    """Keep each accepted timer on its persisted output protocol."""

    def __init__(self, exchange, standard):
        self.exchange, self.standard = exchange, standard
        self.bindings = exchange.bindings

    async def available(self, source):
        if any(self.exchange.identity(key, None, source) for key in self.bindings):
            return await self.exchange.available(source)
        output = await self.standard.available(source)
        if output["output"] in self.bindings:
            # An explicitly configured exchange never silently changes backend.
            raise JobError("unavailable", "The configured enhanced timer output is unavailable")
        return output

    def wake(self):
        self.exchange.wake()
        self.standard.wake()

    def start(self):
        self.exchange.start()
        self.standard.start()

    async def stop(self):
        await asyncio.gather(self.exchange.stop(), self.standard.stop())


def configure_delivery(hass, manager, bindings):
    """Use standard registered outputs; explicit mappings retain enhanced delivery."""
    from .ha_native_timer_output import HAStandardTimerOutput
    from .standard_timer_delivery import StandardTimerDelivery

    exchange = configure_exchange_delivery(hass, manager, bindings)
    standard = StandardTimerDelivery(manager, HAStandardTimerOutput(hass))
    delivery = CombinedTimerDelivery(exchange, standard)
    manager.timer_output = delivery.available
    manager.on_change = delivery.wake
    return delivery
