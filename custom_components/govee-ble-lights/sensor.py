from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from . import Hub
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONNECTION_CLOUD_API = "Cloud API"
CONNECTION_BLUETOOTH = "Bluetooth (Local)"


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        return

    if hub.devices is not None:
        entities = [
            GoveeConnectionTypeSensor(device, CONNECTION_CLOUD_API)
            for device in hub.devices
            if device["type"] == "devices.types.light"
        ]
        async_add_entities(entities)
    elif hub.address is not None:
        model = config_entry.data.get("model")
        async_add_entities([
            GoveeConnectionTypeSensor(None, CONNECTION_BLUETOOTH, address=hub.address, model=model)
        ])


class GoveeConnectionTypeSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Connection type"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:api"

    def __init__(self, device: dict | None, connection_type: str, address: str = None, model: str = None) -> None:
        self._attr_native_value = connection_type

        if device is not None:
            self._device_id = device["device"]
            self._device_name = device["deviceName"]
            self._model = device["sku"]
        else:
            self._device_id = address.replace(":", "")
            self._device_name = "GOVEE Light"
            self._model = model

        self._attr_unique_id = f"{self._device_id}_connection_type"

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Govee",
            "model": self._model,
        }
