"""Home Assistant TTS engine backed by the SmartHome speech service."""
from __future__ import annotations

from homeassistant.components.tts import TextToSpeechEntity, Voice
from homeassistant.exceptions import HomeAssistantError, PlatformNotReady

from .const import DATA_SPEECH_CLIENT, DOMAIN
from .speech import MAX_TTS_TEXT_CHARS, SpeechError


async def async_setup_entry(hass, entry, async_add_entities):
    """Add the TTS entity when the speech service advertises an available backend."""
    client = hass.data[DOMAIN][DATA_SPEECH_CLIENT][entry.entry_id]
    try:
        info = await client.async_get_info()
        tts = info.get("tts")
        if not isinstance(tts, dict) or tts.get("available") is not True:
            raise PlatformNotReady("SmartHome speech service TTS backend is not available")
        languages = _languages(tts.get("languages"))
        voices = _voices(tts.get("voices"))
        default_voice = tts.get("default_voice")
        if (not languages or not voices or not isinstance(default_voice, str)
                or default_voice not in {voice.voice_id for voice in voices}):
            raise PlatformNotReady("SmartHome speech service returned incomplete TTS information")
    except SpeechError as err:
        raise PlatformNotReady("Could not load SmartHome speech service information") from err
    async_add_entities([SmartHomeTextToSpeech(
        client, entry.entry_id, languages, voices, default_voice,
    )])


def _languages(value):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item.strip()))


def _voices(value):
    if not isinstance(value, list):
        return []
    voices = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        voice_id, name = item.get("id"), item.get("name")
        if (not isinstance(voice_id, str) or not voice_id.strip()
                or not isinstance(name, str) or not name.strip() or voice_id in seen):
            continue
        voices.append(Voice(voice_id=voice_id, name=name))
        seen.add(voice_id)
    return voices


class SmartHomeTextToSpeech(TextToSpeechEntity):
    """Generate WAV audio with the configured SmartHome speech service voice."""

    _attr_name = "SmartHome Assistant"
    _attr_supported_options = ["voice"]

    def __init__(self, client, entry_id, languages, voices, default_voice):
        self._client = client
        self._voices = voices
        self._voice_ids = {voice.voice_id for voice in voices}
        self._default_voice = default_voice
        self._attr_unique_id = f"{entry_id}_tts"
        self._attr_supported_languages = languages
        self._attr_default_language = "en" if "en" in languages else languages[0]
        self._attr_default_options = {"voice": default_voice}

    def async_get_supported_voices(self, language):
        if language not in self.supported_languages:
            return []
        return self._voices

    async def async_get_tts_audio(self, message, language, options):
        if language not in self.supported_languages:
            raise HomeAssistantError("SmartHome speech service does not advertise this TTS language")
        if len(message) > MAX_TTS_TEXT_CHARS:
            raise HomeAssistantError("TTS text exceeds the 2000 character limit")
        voice = (options or {}).get("voice") or self._default_voice
        if voice not in self._voice_ids:
            raise HomeAssistantError("SmartHome speech service does not advertise this TTS voice")
        try:
            data = await self._client.async_synthesize(message, language, voice)
        except SpeechError as err:
            raise HomeAssistantError("SmartHome speech service could not synthesize the text") from err
        return "wav", data
