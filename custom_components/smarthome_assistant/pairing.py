"""Short-lived, HA-owned pairing requests; credentials never enter discovery."""
from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import time
from typing import Any
from uuid import uuid4

DATA_PAIRING = "smarthome_assistant_pairing"
PAIRING_TTL = 300
MAX_PENDING = 8
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_SECRET = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")


class PairingError(ValueError):
    """No usable offer belongs to this live HA configuration flow."""


@dataclass
class PendingPairing:
    flow_id: str
    pairing_id: str
    challenge: str
    core_url: str
    expires_at: float
    offer: dict[str, Any] | None = None


class PairingRequests:
    """RAM-only proposals, separate from persisted/approved integration entries."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.pending: dict[str, PendingPairing] = {}

    def _prune(self):
        now = self.clock()
        self.pending = {key: value for key, value in self.pending.items()
                        if now < value.expires_at}

    def begin(self, flow_id: str, core_url: str) -> PendingPairing:
        self._prune()
        for proposal in self.pending.values():
            if proposal.flow_id == flow_id and proposal.core_url == core_url:
                return proposal
        self.remove_flow(flow_id)
        if len(self.pending) >= MAX_PENDING:
            raise PairingError("Too many pairing requests are already open")
        proposal = PendingPairing(flow_id, uuid4().hex, secrets.token_urlsafe(32),
                                  core_url, self.clock() + PAIRING_TTL)
        self.pending[proposal.pairing_id] = proposal
        return proposal

    def receive(self, message: dict) -> dict:
        self._prune()
        pairing_id, challenge = message.get("pairing_id"), message.get("challenge")
        if (not isinstance(pairing_id, str) or not _IDENTIFIER.fullmatch(pairing_id)
                or not isinstance(challenge, str) or not _SECRET.fullmatch(challenge)):
            raise PairingError("Invalid pairing request")
        proposal = self.pending.get(pairing_id)
        if proposal is None or not secrets.compare_digest(proposal.challenge, challenge):
            raise PairingError("No current matching HA pairing request")
        core_id, credential = message.get("core_id"), message.get("credential")
        info, expires_in = message.get("info"), message.get("expires_in")
        if (type(message.get("version")) is not int or message["version"] != 1
                or not isinstance(core_id, str) or not _IDENTIFIER.fullmatch(core_id)
                or not isinstance(credential, str) or not _SECRET.fullmatch(credential)
                or type(expires_in) is not int or not 0 < expires_in <= PAIRING_TTL
                or type(message.get("replacing")) is not bool
                or not isinstance(info, dict) or type(info.get("version")) is not int
                or info["version"] != 1):
            raise PairingError("Invalid Core pairing offer")
        profiles = info.get("profiles")
        if (not isinstance(profiles, list) or not 1 <= len(profiles) <= 64
                or any(not isinstance(profile, dict)
                       or any(not isinstance(profile.get(field), str)
                              or not 0 < len(profile[field]) <= 160
                              for field in ("id", "name")) for profile in profiles)
                or info.get("default_profile") not in {profile["id"] for profile in profiles}):
            raise PairingError("Invalid Core profile metadata")
        # Store only the agreed fields; WS ids and other transport data do not
        # become configuration or authorization. A duplicate cannot replace it.
        offer = {key: message[key] for key in (
            "version", "pairing_id", "challenge", "core_id", "credential",
            "expires_in", "replacing", "info")}
        offer["info"] = {"version": 1, "default_profile": info["default_profile"],
                         "profiles": [{key: profile[key] for key in ("id", "name")}
                                      for profile in profiles]}
        if proposal.offer is not None and proposal.offer != offer:
            raise PairingError("The pending Core offer changed")
        if proposal.offer is None:
            proposal.offer = offer
            proposal.expires_at = min(proposal.expires_at, self.clock() + expires_in)
        return {"accepted": True, "pairing_id": pairing_id}

    def get(self, pairing_id: str) -> PendingPairing:
        self._prune()
        proposal = self.pending.get(pairing_id)
        if proposal is None or proposal.offer is None:
            raise PairingError("Pairing expired or no authenticated offer arrived")
        return proposal

    def remove_flow(self, flow_id: str):
        self.pending = {key: value for key, value in self.pending.items()
                        if value.flow_id != flow_id}


def pairing_requests(hass) -> PairingRequests:
    """Register the admin-only receiver before a first-run flow contacts Core."""
    if DATA_PAIRING in hass.data:
        return hass.data[DATA_PAIRING]
    from homeassistant.components import websocket_api
    import voluptuous as vol

    requests = PairingRequests()
    hass.data[DATA_PAIRING] = requests

    @websocket_api.websocket_command({
        vol.Required("id"): int,
        vol.Required("type"): "smarthome_assistant/pairing_offer",
        vol.Required("version"): int,
        vol.Required("pairing_id"): str,
        vol.Required("challenge"): str,
        vol.Required("core_id"): str,
        vol.Required("token"): str,
        vol.Required("expires_in"): int,
        vol.Required("replacing"): bool,
        vol.Required("info"): dict,
    })
    @websocket_api.require_admin
    @websocket_api.async_response
    async def receive_offer(_hass, connection, message):
        try:
            result = requests.receive({**message, "credential": message["token"]})
        except PairingError:
            connection.send_error(message["id"], "invalid_pairing", "No valid matching HA pairing request")
        else:
            connection.send_result(message["id"], result)

    websocket_api.async_register_command(hass, receive_offer)
    return requests
