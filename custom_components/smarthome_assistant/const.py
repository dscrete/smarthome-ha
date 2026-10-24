"""Constants for the SmartHome Assistant conversation integration."""
from typing import Any

DOMAIN = "smarthome_assistant"
CONVERSATION_PLATFORM = "conversation"
SENSOR_PLATFORM = "sensor"
SPEECH_PLATFORMS = ("stt", "tts")
PLATFORMS = (CONVERSATION_PLATFORM, SENSOR_PLATFORM, *SPEECH_PLATFORMS)

CONF_CORE_URL = "core_url"
CONF_TOKEN = "token"
CONF_AUTH_METHOD = "auth_method"
CONF_PAIRING_CORE_ID = "pairing_core_id"
CONF_PROFILE_ID = "profile_id"
CONF_TIMER_OUTPUTS = "timer_outputs"
CONF_SPEECH_ENABLED = "speech_enabled"
CONF_SPEECH_URL = "speech_url"

DATA_SPEECH_CLIENT = "speech_client"

_TIMER_EXCHANGE_SUFFIX = "_smarthome_timer_exchange_v1"


def validate_timer_outputs(value: Any) -> dict[str, str]:
    """Normalize the configured satellite-to-ESPHome timer output mapping."""
    if not isinstance(value, dict):
        raise ValueError("Timer outputs must be an object")

    result = {}
    outputs = set()
    for satellite, output in value.items():
        if not isinstance(satellite, str) or not isinstance(output, str):
            raise ValueError("Timer output entity IDs must be strings")
        satellite = satellite.strip()
        output = output.strip()
        if (not satellite.startswith("assist_satellite.")
                or not satellite.partition(".")[2]
                or "." in satellite.partition(".")[2]):
            raise ValueError("Timer output keys must be assist_satellite entities")
        if (not output.startswith("esphome.")
                or not output.partition(".")[2]
                or "." in output.partition(".")[2]
                or not output.endswith(_TIMER_EXCHANGE_SUFFIX)):
            raise ValueError("Timer outputs must be ESPHome timer exchange entities")
        if output in outputs:
            raise ValueError("Each ESPHome timer output can be assigned only once")
        result[satellite] = output
        outputs.add(output)
    return result

DEFAULT_REQUEST_TIMEOUT = 150
CORE_PROTOCOL_VERSION = 1
