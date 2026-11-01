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
CONF_SHOW_IN_SIDEBAR = "show_in_sidebar"
CONF_DEFAULT_ALERT_OUTPUT = "default_alert_output"
CONF_SNOOZE_SECONDS = "snooze_seconds"
DEFAULT_SNOOZE_SECONDS = 9 * 60
CONF_MEDIA_PLAYER_MAP = "media_player_map"
CONF_MEDIA_MAX_VOLUME = "media_max_volume"
DEFAULT_MEDIA_MAX_VOLUME = 100
DATA_MEDIA = "smarthome_assistant_media"


def alert_options(options: dict) -> dict:
    """Validate HA-owned alert preferences; existing jobs retain their bindings."""
    output = options.get(CONF_DEFAULT_ALERT_OUTPUT, "")
    seconds = options.get(CONF_SNOOZE_SECONDS, DEFAULT_SNOOZE_SECONDS)
    if not isinstance(output, str):
        raise ValueError("Default alert output must be an Assist satellite")
    output = output.strip()
    if output and (not output.startswith("assist_satellite.")
                   or not output.partition(".")[2] or output.count(".") != 1
                   or not all(char.isalnum() or char == "_" for char in output.partition(".")[2])):
        raise ValueError("Default alert output must be an Assist satellite")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 86400 or int(seconds) != seconds:
        raise ValueError("Snooze must be whole seconds between 1 and 86400")
    return {CONF_DEFAULT_ALERT_OUTPUT: output, CONF_SNOOZE_SECONDS: int(seconds)}

def _entity_id(value: Any, domain: str) -> bool:
    if not isinstance(value, str):
        return False
    prefix, _, name = value.strip().partition(".")
    return (prefix == domain and bool(name) and "." not in name
            and all(char.isalnum() or char == "_" for char in name))


def media_options(options: dict) -> dict:
    """Validate optional speaker overrides and the voice volume ceiling.

    Speakers are discovered from Assist exposure, areas and aliases; the map
    only overrides which speaker a satellite means when that discovery is wrong.
    """
    mapping = options.get(CONF_MEDIA_PLAYER_MAP)
    mapping = {} if mapping is None else mapping
    ceiling = options.get(CONF_MEDIA_MAX_VOLUME, DEFAULT_MEDIA_MAX_VOLUME)
    if not isinstance(mapping, dict):
        raise ValueError("Speaker overrides must be an object")
    result = {}
    for satellite, player in mapping.items():
        if not _entity_id(satellite, "assist_satellite") or not _entity_id(player, "media_player"):
            raise ValueError("Speaker overrides map assist_satellite entities to media_player entities")
        result[satellite.strip()] = player.strip()
    if (isinstance(ceiling, bool) or not isinstance(ceiling, (int, float))
            or int(ceiling) != ceiling or not 1 <= ceiling <= 100):
        raise ValueError("Maximum voice volume must be a whole percentage between 1 and 100")
    return {CONF_MEDIA_PLAYER_MAP: result, CONF_MEDIA_MAX_VOLUME: int(ceiling)}

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
