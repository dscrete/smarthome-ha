"""Administrator-only WebSocket commands for SmartHome music and volume.

Expected outcomes (no speaker, ambiguous speaker, nothing found, Music
Assistant unavailable) are results with a ``status`` so Core can speak or
clarify them. Malformed requests are WebSocket errors.
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components import websocket_api

from .const import DATA_MEDIA, DOMAIN
from .media import CONTROL_SERVICES, ENQUEUE_OPTIONS, SPEAKER_OPERATIONS, VOLUME_MODES, MediaError
from .media_match import KINDS
from .media_targets import TargetError

_LOGGER = logging.getLogger(__name__)
_PREFIX = f"{DOMAIN}/media"
_BASE = {vol.Required("id"): int}
_REQUEST = {vol.Required("request_id"): vol.All(str, vol.Length(min=1, max=128))}
_SOURCE = {vol.Optional("source_satellite"): vol.Any(None, str),
           vol.Optional("device_id"): vol.Any(None, str)}
_TARGET = {vol.Optional("targets"): vol.All([str], vol.Length(max=16)),
           vol.Optional("scope"): vol.In(("source", "all", "playing"))}


def _broker(hass):
    return hass.data[DATA_MEDIA]


async def _send(hass, connection, msg, command, operation):
    try:
        if command is None:
            result = await operation()
        else:
            result = await _broker(hass).run(command, msg["request_id"], operation)
        connection.send_result(msg["id"], result)
    except TargetError as error:
        connection.send_result(msg["id"], {"status": error.code, "message": str(error),
                                           "choices": error.choices})
    except MediaError as error:
        connection.send_result(msg["id"], {"status": error.code, "message": str(error), **error.details})
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_format", str(error))
    except Exception:
        _LOGGER.exception("SmartHome media command failed")
        connection.send_error(msg["id"], "unknown_error", "Media command failed")


@websocket_api.websocket_command({**_BASE, **_SOURCE, vol.Required("type"): f"{_PREFIX}/info"})
@websocket_api.require_admin
@websocket_api.async_response
async def info(hass, connection, msg):
    await _send(hass, connection, msg, None, lambda: _broker(hass).info(msg))


@websocket_api.websocket_command({
    **_BASE, **_REQUEST, **_SOURCE, **_TARGET,
    vol.Required("kind"): vol.In(KINDS),
    vol.Required("query"): str,
    vol.Optional("artist"): vol.Any(None, str),
    vol.Optional("enqueue"): vol.In(ENQUEUE_OPTIONS),
    vol.Optional("radio_mode"): vol.Any(None, bool),
    vol.Required("type"): f"{_PREFIX}/play",
})
@websocket_api.require_admin
@websocket_api.async_response
async def play(hass, connection, msg):
    await _send(hass, connection, msg, "play", lambda: _broker(hass).play(msg))


@websocket_api.websocket_command({
    **_BASE, **_REQUEST, **_SOURCE, **_TARGET,
    vol.Required("operation"): vol.In(SPEAKER_OPERATIONS),
    vol.Optional("session"): str,
    vol.Required("type"): f"{_PREFIX}/speakers",
})
@websocket_api.require_admin
@websocket_api.async_response
async def speakers(hass, connection, msg):
    await _send(hass, connection, msg, "speakers", lambda: _broker(hass).speakers(msg))


@websocket_api.websocket_command({
    **_BASE, **_REQUEST, **_SOURCE, **_TARGET,
    vol.Required("mode"): vol.In(VOLUME_MODES),
    vol.Optional("value"): vol.All(int, vol.Range(min=-100, max=100)),
    vol.Required("type"): f"{_PREFIX}/volume",
})
@websocket_api.require_admin
@websocket_api.async_response
async def volume(hass, connection, msg):
    await _send(hass, connection, msg, "volume", lambda: _broker(hass).volume(msg))


@websocket_api.websocket_command({
    **_BASE, **_REQUEST, **_SOURCE, **_TARGET,
    vol.Required("operation"): vol.In(tuple(CONTROL_SERVICES)),
    vol.Required("type"): f"{_PREFIX}/control",
})
@websocket_api.require_admin
@websocket_api.async_response
async def control(hass, connection, msg):
    await _send(hass, connection, msg, "control", lambda: _broker(hass).control(msg))


def async_register_media_commands(hass):
    for command in (info, play, speakers, volume, control):
        websocket_api.async_register_command(hass, command)


def async_setup_media(hass):
    """Create the broker and register its commands once per HA run."""
    from .media import MediaBroker

    if DATA_MEDIA not in hass.data:
        hass.data[DATA_MEDIA] = MediaBroker(hass)
        async_register_media_commands(hass)
    return hass.data[DATA_MEDIA]
