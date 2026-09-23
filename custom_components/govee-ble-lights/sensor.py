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
CONNECTION_LAN = "LAN (Local)"
CONNECTION_HYBRID_LAN = "Hybrid (LAN + Cloud)"


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        return

    if hub.devices is not None:
        entities = []
        for device in hub.devices:
            if device["type"] != "devices.types.light":
                continue

            ip_address = None
            if hub.lan_controller is not None:
                lan_match = next(
                    (d for d in hub.lan_controller.devices if d.fingerprint == device["device"]), None
                )
                if lan_match is not None:
                    ip_address = lan_match.ip

            connection_type = CONNECTION_HYBRID_LAN if ip_address is not None else CONNECTION_CLOUD_API

            entities.append(GoveeConnectionTypeSensor(device, connection_type, ip_address=ip_address))
        async_add_entities(entities)
    elif hub.address is not None:
        model = config_entry.data.get("model")
        async_add_entities([
            GoveeConnectionTypeSensor(None, CONNECTION_BLUETOOTH, address=hub.address, model=model)
        ])
    elif hub.lan_controller is not None:
        async_add_entities([
            GoveeConnectionTypeSensor(None, CONNECTION_LAN, lan_device=device)
            for device in hub.lan_controller.devices
        ])


class GoveeConnectionTypeSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Connection type"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:api"

    def __init__(self, device: dict | None, connection_type: str, address: str = None, model: str = None,
                 lan_device=None, ip_address: str = None) -> None:
        self._attr_native_value = connection_type
        self._ip_address = ip_address

        if device is not None:
            self._device_id = device["device"]
            self._device_name = device["deviceName"]
            self._model = device["sku"]
        elif lan_device is not None:
            self._device_id = lan_device.fingerprint
            self._device_name = "GOVEE Light"
            self._model = lan_device.sku
            self._ip_address = lan_device.ip
        else:
            self._device_id = address.replace(":", "")
            self._device_name = "GOVEE Light"
            self._model = model

        self._attr_unique_id = f"{self._device_id}_connection_type"

    @property
    def extra_state_attributes(self) -> dict | None:
        attributes = {}
        if self._ip_address is not None:
            attributes["ip_address"] = self._ip_address
        return attributes or None

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Govee",
            "model": self._model,
        }
