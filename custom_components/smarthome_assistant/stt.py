"""Home Assistant STT engine backed by the SmartHome speech service."""
from __future__ import annotations

from collections.abc import AsyncIterable
import io
import wave

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.exceptions import HomeAssistantError, PlatformNotReady

from .const import DATA_SPEECH_CLIENT, DOMAIN
from .speech import MAX_STT_AUDIO_BYTES, SpeechError


async def async_setup_entry(hass, entry, async_add_entities):
    """Add the speech entity when the speech service advertises an STT backend."""
    client = hass.data[DOMAIN][DATA_SPEECH_CLIENT][entry.entry_id]
    try:
        info = await client.async_get_info()
        stt = info.get("stt")
        if not isinstance(stt, dict) or stt.get("available") is not True:
            raise PlatformNotReady("SmartHome speech service STT backend is not available")
        languages = _languages(stt.get("languages"))
        if not languages:
            raise PlatformNotReady("SmartHome speech service STT backend advertises no supported languages")
    except SpeechError as err:
        raise PlatformNotReady("Could not load SmartHome speech service information") from err
    async_add_entities([SmartHomeSpeechToText(client, entry.entry_id, languages)])


def _languages(value):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item.strip()))


class SmartHomeSpeechToText(SpeechToTextEntity):
    """Transcribe 16 kHz PCM mono WAV through the authenticated speech service."""

    _attr_name = "SmartHome Assistant"

    @property
    def supported_languages(self) -> list[str]:
        return self._languages

    @property
    def supported_formats(self) -> list[AudioFormats]:
        return [AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        return [AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        return [AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        return [AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[AudioChannels]:
        return [AudioChannels.CHANNEL_MONO]

    def __init__(self, client, entry_id, languages):
        self._client = client
        self._attr_unique_id = f"{entry_id}_stt"
        self._languages = languages

    async def async_process_audio_stream(self, metadata, stream: AsyncIterable[bytes]) -> SpeechResult:
        if not self.check_metadata(metadata):
            raise HomeAssistantError("SmartHome speech service supports only 16 kHz mono PCM WAV audio")
        # The service limit includes the 44-byte WAV header.
        raw = bytearray()
        max_pcm_bytes = MAX_STT_AUDIO_BYTES - 44
        async for chunk in stream:
            if not isinstance(chunk, bytes):
                raise HomeAssistantError("Home Assistant provided invalid STT audio")
            if len(raw) + len(chunk) > max_pcm_bytes:
                raise HomeAssistantError("STT audio exceeds the 8 MiB request limit")
            raw.extend(chunk)
        if not raw or len(raw) % 2:
            raise HomeAssistantError("STT audio is empty or has an incomplete PCM sample")

        wav = io.BytesIO()
        with wave.open(wav, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(raw)
        try:
            result = await self._client.async_transcribe(wav.getvalue(), metadata.language)
        except SpeechError as err:
            raise HomeAssistantError("SmartHome speech service could not transcribe the audio") from err
        text = result.get("text")
        if not isinstance(text, str):
            raise HomeAssistantError("SmartHome speech service returned an invalid transcription")
        return SpeechResult(text=text, result=SpeechResultState.SUCCESS)
