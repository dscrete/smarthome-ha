"""Authenticated WebSocket command surface for SmartHome jobs."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.helpers import entity_registry as er

from .jobs import DOMAIN, JobError

_BASE = {vol.Required("id"): int}
_REQUEST = {vol.Required("request_id"): str}
_JOB_REV = {vol.Required("job_id"): str, vol.Required("expected_revision"): int}


def _manager(hass):
    return hass.data[DOMAIN]


def _respond(connection, command_id, awaitable):
    async def send():
        try:
            connection.send_result(command_id, {"job": await awaitable})
        except JobError as exc:
            connection.send_error(command_id, exc.code, str(exc))
        except Exception:
            connection.send_error(command_id, "unknown_error", "Job command failed")
    return send()


@websocket_api.websocket_command({**_BASE, **_REQUEST, vol.Required("job"): dict,
                                  vol.Required("type"): f"{DOMAIN}/create"})
@websocket_api.require_admin
@websocket_api.async_response
async def create(hass, connection, msg):
    await _respond(connection, msg["id"], _manager(hass).async_create(msg["request_id"], msg["job"]))


@websocket_api.websocket_command({**_BASE, **_REQUEST, **_JOB_REV,
                                  vol.Required("changes"): dict,
                                  vol.Required("type"): f"{DOMAIN}/update"})
@websocket_api.require_admin
@websocket_api.async_response
async def update(hass, connection, msg):
    await _respond(connection, msg["id"], _manager(hass).async_update(
        msg["request_id"], msg["job_id"], msg["expected_revision"], msg["changes"]))


@websocket_api.websocket_command({**_BASE, **_REQUEST, **_JOB_REV,
                                  vol.Required("type"): f"{DOMAIN}/cancel"})
@websocket_api.require_admin
@websocket_api.async_response
async def cancel(hass, connection, msg):
    await _respond(connection, msg["id"], _manager(hass).async_cancel(
        msg["request_id"], msg["job_id"], msg["expected_revision"]))


@websocket_api.websocket_command({**_BASE,
                                  vol.Optional("statuses"): [str],
                                  vol.Optional("kinds"): [str],
                                  vol.Optional("source_satellite"): str,
                                  vol.Optional("for_reconcile"): bool,
                                  vol.Required("type"): f"{DOMAIN}/list"})
@websocket_api.require_admin
@websocket_api.async_response
async def list_jobs(hass, connection, msg):
    jobs = await _manager(hass).async_list(
        statuses=msg.get("statuses"), kinds=msg.get("kinds"),
        source_satellite=msg.get("source_satellite"),
        for_reconcile=msg.get("for_reconcile", False))
    connection.send_result(msg["id"], {"jobs": jobs})


@websocket_api.websocket_command({**_BASE, vol.Required("source_satellite"): str,
                                  vol.Required("type"): f"{DOMAIN}/timer_output"})
@websocket_api.require_admin
@websocket_api.async_response
async def timer_output(hass, connection, msg):
    try:
        output = await _manager(hass).delivery_worker.available(msg["source_satellite"])
    except JobError as exc:
        connection.send_error(msg["id"], exc.code, str(exc))
    except Exception:
        connection.send_error(msg["id"], "unavailable", "Native timer output is unavailable")
    else:
        connection.send_result(msg["id"], output)


@websocket_api.websocket_command({**_BASE, vol.Required("entity_id"): str,
                                  vol.Required("type"): f"{DOMAIN}/check_exposure"})
@websocket_api.require_admin
@websocket_api.async_response
async def check_exposure(hass, connection, msg):
    """Use HA's current Assist policy, independent of Core's cached catalog."""
    entity_id = msg["entity_id"]
    if hass.states.get(entity_id) is None:
        connection.send_result(msg["id"], {"exposed": False})
        return
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.hidden_by is not None:
        connection.send_result(msg["id"], {"exposed": False})
        return
    try:
        exposed = async_should_expose(hass, "conversation", entity_id)
    except (KeyError, ValueError):
        connection.send_error(msg["id"], "unavailable", "Assist exposure is unavailable")
        return
    connection.send_result(msg["id"], {"exposed": exposed})


@websocket_api.websocket_command({**_BASE, **_REQUEST, **_JOB_REV,
                                  vol.Required("type"): f"{DOMAIN}/claim"})
@websocket_api.require_admin
@websocket_api.async_response
async def claim(hass, connection, msg):
    await _respond(connection, msg["id"], _manager(hass).async_claim(
        msg["request_id"], msg["job_id"], msg["expected_revision"]))


@websocket_api.websocket_command({**_BASE, **_REQUEST, **_JOB_REV,
                                  vol.Required("outcome"): str,
                                  vol.Required("type"): f"{DOMAIN}/complete"})
@websocket_api.require_admin
@websocket_api.async_response
async def complete(hass, connection, msg):
    await _respond(connection, msg["id"], _manager(hass).async_complete(
        msg["request_id"], msg["job_id"], msg["expected_revision"], msg["outcome"]))


@websocket_api.websocket_command({**_BASE, **_REQUEST, **_JOB_REV,
                                  vol.Required("type"): f"{DOMAIN}/timer_dismiss"})
@websocket_api.require_admin
@websocket_api.async_response
async def timer_dismiss(hass, connection, msg):
    await _respond(connection, msg["id"], _manager(hass).async_dismiss_timer(
        msg["request_id"], msg["job_id"], msg["expected_revision"]))


@websocket_api.websocket_command({**_BASE, **_REQUEST, **_JOB_REV,
                                  vol.Required("type"): f"{DOMAIN}/timer_delivered"})
@websocket_api.require_admin
@websocket_api.async_response
async def timer_delivered(hass, connection, msg):
    await _respond(connection, msg["id"], _manager(hass).async_timer_delivered(
        msg["request_id"], msg["job_id"], msg["expected_revision"]))


@websocket_api.websocket_command({**_BASE, **_REQUEST, **_JOB_REV,
                                  vol.Required("type"): f"{DOMAIN}/ack_notice"})
@websocket_api.require_admin
@websocket_api.async_response
async def ack_notice(hass, connection, msg):
    await _respond(connection, msg["id"], _manager(hass).async_ack_notice(
        msg["request_id"], msg["job_id"], msg["expected_revision"]))


def async_register_websocket_commands(hass):
    """Register commands on Home Assistant's already authenticated WS API."""
    for command in (create, update, cancel, list_jobs, check_exposure, timer_output,
                    claim, complete, timer_dismiss, timer_delivered, ack_notice):
        websocket_api.async_register_command(hass, command)
