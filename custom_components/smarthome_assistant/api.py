"""Authenticated HTTP client for Assistant Core's HA adapter protocol."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import aiohttp

from .const import CORE_PROTOCOL_VERSION, DEFAULT_REQUEST_TIMEOUT


class CoreError(Exception):
    """Base class for Core communication failures."""


class CoreAuthError(CoreError):
    """Core rejected the configured bearer credential."""


class CoreUnavailableError(CoreError):
    """Core could not return a valid protocol response."""


class CoreProtocolError(CoreUnavailableError):
    """Core is reachable but speaks an unsupported protocol version."""


class CoreTimeoutError(CoreError):
    """Core did not finish before the request deadline."""


def normalize_core_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Core URL must be an absolute HTTP(S) URL")
    if (parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ValueError("Core URL cannot contain user information, query, or fragment")
    return url


class CoreClient:
    def __init__(self, session, core_url: str, token: str, timeout=DEFAULT_REQUEST_TIMEOUT):
        self.session = session
        self.core_url = normalize_core_url(core_url)
        self.token = token
        self.timeout = timeout

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def async_get_info(self) -> dict:
        return await self._request("GET", "/ha/v1/info")

    async def async_turn(self, payload: dict) -> dict:
        return await self._request("POST", "/ha/v1/turn", json=payload)

    async def async_request_pairing(self, pairing_id: str, challenge: str) -> dict:
        return await self._request("POST", "/ha/v1/pairing/request", json={
            "pairing_id": pairing_id, "challenge": challenge})

    async def async_complete_pairing(self, pairing_id: str, challenge: str) -> dict:
        result = await self._request("POST", "/ha/v1/pairing/complete", json={
            "pairing_id": pairing_id, "challenge": challenge})
        if result.get("version") != 1 or result.get("status") != "paired":
            raise CoreProtocolError("Core did not confirm pairing")
        return result

    async def async_revoke_pairing(self) -> dict:
        return await self._request("DELETE", "/ha/v1/pairing")

    async def _request(self, method: str, path: str, *, json=None) -> dict:
        try:
            async with self.session.request(
                method,
                f"{self.core_url}{path}",
                headers=self.headers if self.token else {"Content-Type": "application/json"},
                json=json,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status in (401, 403):
                    raise CoreAuthError("Core rejected the configured bearer token")
                if response.status >= 500:
                    raise CoreUnavailableError("Core returned a server error")
                if response.status < 200 or response.status >= 300:
                    raise CoreUnavailableError(f"Core rejected the request (HTTP {response.status})")
                try:
                    data = await response.json()
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise CoreUnavailableError("Core returned an invalid response") from err
        except (CoreAuthError, CoreUnavailableError):
            raise
        except (asyncio.TimeoutError, TimeoutError) as err:
            raise CoreTimeoutError("Core request timed out") from err
        except aiohttp.ClientError as err:
            raise CoreUnavailableError("Core is unavailable") from err
        if not isinstance(data, dict):
            raise CoreUnavailableError("Core returned an invalid response")
        return data

    @staticmethod
    def validate_info(data: dict) -> None:
        if data.get("version") != CORE_PROTOCOL_VERSION:
            raise CoreProtocolError("Core protocol version is unsupported")

    @staticmethod
    def validate_turn(data: dict) -> dict:
        if data.get("version") != CORE_PROTOCOL_VERSION:
            raise CoreProtocolError("Core protocol version is unsupported")
        if not isinstance(data.get("speech"), str):
            raise CoreUnavailableError("Core response is missing speech")
        if not isinstance(data.get("status"), str):
            raise CoreUnavailableError("Core response is missing status")
        if not isinstance(data.get("continue_conversation"), bool):
            raise CoreUnavailableError("Core response has invalid continuation state")
        conversation_id = data.get("conversation_id")
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise CoreUnavailableError("Core response has invalid conversation id")
        return data
