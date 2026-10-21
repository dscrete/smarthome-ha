"""SmartHome Assistant ConversationEntity implementation."""
from __future__ import annotations

from uuid import uuid4
import logging

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry, device_registry, entity_registry, intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry

from .api import CoreAuthError, CoreClient, CoreError, CoreTimeoutError
from .const import CONF_BINDINGS, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    client: CoreClient = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SmartHomeConversationEntity(hass, entry, client)])


class SmartHomeConversationEntity(conversation.ConversationEntity):
    _attr_name = "SmartHome Assistant"
    _attr_should_poll = False
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: CoreClient):
        self.hass = hass
        self.entry = entry
        self.client = client
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> str:
        return "*"

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        source = self._trusted_source(user_input)
        # HA owns this delivered log's identity; Core owns its separate bounded
        # memory. Never return a different ID from the log we append to.
        conversation_id = chat_log.conversation_id
        payload = {
            "request_id": str(uuid4()),
            "agent_id": self.entry.entry_id,
            "conversation_id": conversation_id,
            "text": user_input.text,
            "language": user_input.language,
            **source,
        }
        try:
            data = self.client.validate_turn(await self.client.async_turn(payload))
            speech = data["speech"]
            continue_conversation = data["continue_conversation"]
        except CoreTimeoutError:
            speech = "The result is unknown because SmartHome Core timed out. I will not retry that request."
            continue_conversation = False
        except CoreAuthError:
            speech = "SmartHome Core rejected its configured credentials."
            continue_conversation = False
        except CoreError as error:
            _LOGGER.warning("SmartHome Core turn unavailable: %s", error)
            speech = "SmartHome Core is unavailable."
            continue_conversation = False

        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=user_input.agent_id,
                content=speech,
            )
        )
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(speech)
        return conversation.ConversationResult(
            conversation_id=conversation_id,
            response=response,
            continue_conversation=continue_conversation,
        )

    def _trusted_source(self, user_input: conversation.ConversationInput) -> dict:
        """Resolve source and output routing only from ConversationInput + HA registries."""
        device_id = user_input.device_id
        device_reg = device_registry.async_get(self.hass)
        entity_reg = entity_registry.async_get(self.hass)
        area_reg = area_registry.async_get(self.hass)

        device = device_reg.async_get(device_id) if isinstance(device_id, str) else None
        registered_device_id = device.id if device else None
        associated = (
            entity_registry.async_entries_for_device(entity_reg, registered_device_id)
            if registered_device_id else []
        )
        satellite_entities = {
            item.entity_id for item in associated
            if item.domain == "assist_satellite"
        }
        input_satellite = user_input.satellite_id
        satellite_entry = None
        if isinstance(input_satellite, str):
            satellite_entry = entity_reg.async_get(input_satellite)
            if satellite_entry and satellite_entry.domain == "assist_satellite":
                provided_device_id = user_input.device_id
                if (isinstance(provided_device_id, str)
                        and satellite_entry.device_id != provided_device_id):
                    input_satellite = None
                    satellite_entry = None
                else:
                    satellite_entities.add(satellite_entry.entity_id)
                    if registered_device_id is None and satellite_entry.device_id:
                        satellite_device = device_reg.async_get(satellite_entry.device_id)
                        if satellite_device:
                            device = satellite_device
                            registered_device_id = satellite_device.id
                            associated = entity_registry.async_entries_for_device(
                                entity_reg, registered_device_id,
                            )
            else:
                input_satellite = None

        area_id = None
        # The source satellite's explicit HA area is the most specific room signal.
        if satellite_entry and satellite_entry.area_id:
            if area_reg.async_get_area(satellite_entry.area_id):
                area_id = satellite_entry.area_id
        candidate_area = getattr(user_input, "area_id", None)
        if area_id is None and isinstance(candidate_area, str) and area_reg.async_get_area(candidate_area):
            area_id = candidate_area
        if area_id is None and device and device.area_id:
            area_id = device.area_id
        area = area_reg.async_get_area(area_id) if area_id else None

        bindings = self.entry.options.get(
            CONF_BINDINGS, self.entry.data.get(CONF_BINDINGS, {}),
        )
        output_binding = None
        if isinstance(bindings, dict):
            for key in ([input_satellite] if input_satellite else []) + sorted(satellite_entities) + (
                [registered_device_id] if registered_device_id else []
            ):
                value = bindings.get(key)
                if isinstance(value, str) and value.strip():
                    output_binding = value.strip()
                    break

        result = {}
        if registered_device_id:
            result["device_id"] = registered_device_id
        if input_satellite:
            result["satellite_id"] = input_satellite
        if area:
            result["area_id"] = area.id
            result["area_name"] = area.name
        if output_binding:
            result["output_binding"] = output_binding
        return result
