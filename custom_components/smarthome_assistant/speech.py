"""Authenticated client for the optional SmartHome speech service."""
from __future__ import annotations

import asyncio
import importlib
import json
from urllib.parse import urlsplit, urlunsplit

from .const import CONF_CORE_URL, CONF_SPEECH_URL, CONF_TOKEN

MAX_STT_AUDIO_BYTES = 8 * 1024 * 1024
MAX_TTS_AUDIO_BYTES = 32 * 1024 * 1024
MAX_TTS_TEXT_CHARS = 2000
SPEECH_TIMEOUT = 120


class SpeechError(Exception):
    """Base class for speech service failures."""


class SpeechUnavailableError(SpeechError):
    """The speech service could not complete a valid request."""


def _aiohttp():
    """Load aiohttp only when making requests (HA supplies this dependency)."""
    return importlib.import_module("aiohttp")


def _base_url(value: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ValueError("Speech URL must be an absolute HTTP(S) URL without credentials, query, or fragment")
    try:
        port = parts.port
    except ValueError as err:
        raise ValueError("Speech URL has an invalid port") from err
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def speech_base_url(core_url: str, override: str = "") -> str:
    """Keep an explicit reverse-proxy prefix; otherwise use Core's host on 8773."""
    if override.strip():
        return _base_url(override)
    parts = urlsplit(core_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Core URL must be an absolute HTTP(S) URL")
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:8773"


def speech_base_url_for_entry(entry) -> str:
    """Resolve the speech origin from options, then entry data, then Core URL."""
    override = entry.options.get(CONF_SPEECH_URL, entry.data.get(CONF_SPEECH_URL, ""))
    return speech_base_url(entry.data[CONF_CORE_URL], override or "")


class SpeechClient:
    """Small authenticated HTTP client; redirects are deliberately never followed."""

    def __init__(self, session, base_url: str, token: str, timeout: int = SPEECH_TIMEOUT):
        self.session = session
        self.base_url = _base_url(base_url)
        self.token = token
        self.timeout = timeout

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    async def async_get_info(self) -> dict:
        aiohttp = _aiohttp()
        try:
            async with self.session.get(
                f"{self.base_url}/ha/v1/speech/info", headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=min(self.timeout, 15)), allow_redirects=False,
            ) as response:
                body = await self._read_limited(response, 64 * 1024)
                if response.status < 200 or response.status >= 300:
                    raise SpeechUnavailableError(f"SmartHome speech service rejected the request (HTTP {response.status})")
                try:
                    value = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError) as err:
                    raise SpeechUnavailableError("SmartHome speech service returned invalid service information") from err
        except SpeechError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as err:
            raise SpeechUnavailableError("SmartHome speech service is unavailable") from err
        if not isinstance(value, dict) or value.get("version") != 1:
            raise SpeechUnavailableError("SmartHome speech service returned an unsupported response")
        return value

    async def async_transcribe(self, audio: bytes, language: str) -> dict:
        aiohttp = _aiohttp()
        if len(audio) > MAX_STT_AUDIO_BYTES:
            raise SpeechUnavailableError("STT audio exceeds the 8 MiB request limit")
        form = aiohttp.FormData()
        form.add_field("file", audio, filename="audio.wav", content_type="audio/wav")
        form.add_field("language", language)
        return await self._request_json("POST", "/ha/v1/stt", data=form)

    async def async_synthesize(self, text: str, locale: str, voice: str | None) -> bytes:
        aiohttp = _aiohttp()
        if len(text) > MAX_TTS_TEXT_CHARS:
            raise SpeechUnavailableError("TTS text exceeds the 2000 character limit")
        payload = {"text": text, "locale": locale}
        if voice:
            payload["voice"] = voice
        try:
            async with self.session.post(
                f"{self.base_url}/ha/v1/tts", headers={**self.headers, "Content-Type": "application/json"},
                json=payload, timeout=aiohttp.ClientTimeout(total=self.timeout), allow_redirects=False,
            ) as response:
                if response.status < 200 or response.status >= 300:
                    await self._read_limited(response, 64 * 1024)
                    raise SpeechUnavailableError(f"SmartHome speech service rejected the request (HTTP {response.status})")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "audio/wav":
                    raise SpeechUnavailableError("SmartHome speech service did not return WAV audio")
                data = await self._read_limited(response, MAX_TTS_AUDIO_BYTES)
        except SpeechError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as err:
            raise SpeechUnavailableError("SmartHome speech service is unavailable") from err
        if not data:
            raise SpeechUnavailableError("SmartHome speech service returned empty audio")
        return data

    async def _request_json(self, method: str, path: str, **kwargs) -> dict:
        aiohttp = _aiohttp()
        try:
            async with self.session.request(
                method, f"{self.base_url}{path}", headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout), allow_redirects=False, **kwargs,
            ) as response:
                body = await self._read_limited(response, 1024 * 1024)
                if response.status < 200 or response.status >= 300:
                    raise SpeechUnavailableError(f"SmartHome speech service rejected the request (HTTP {response.status})")
                try:
                    value = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError) as err:
                    raise SpeechUnavailableError("SmartHome speech service returned invalid transcription data") from err
        except SpeechError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as err:
            raise SpeechUnavailableError("SmartHome speech service is unavailable") from err
        if not isinstance(value, dict):
            raise SpeechUnavailableError("SmartHome speech service returned invalid transcription data")
        return value

    @staticmethod
    async def _read_limited(response, limit: int) -> bytes:
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                if int(length) > limit:
                    raise SpeechUnavailableError("SmartHome speech service response exceeded its size limit")
            except ValueError:
                raise SpeechUnavailableError("SmartHome speech service returned an invalid content length")
        chunks = []
        size = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > limit:
                raise SpeechUnavailableError("SmartHome speech service response exceeded its size limit")
            chunks.append(chunk)
        return b"".join(chunks)
