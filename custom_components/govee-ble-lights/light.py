from __future__ import annotations

import array
import logging
import re

from enum import IntEnum
import bleak_retry_connector

from bleak import BleakClient
from homeassistant.components import bluetooth
from homeassistant.components.light import (ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_EFFECT, ColorMode, LightEntity,
                                            LightEntityFeature, ATTR_COLOR_TEMP_KELVIN)

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store
import homeassistant.util.color as color_util

from .const import DOMAIN
from pathlib import Path
import json
from .govee_utils import prepareMultiplePacketsData
import base64
from . import Hub
from datetime import timedelta

SCAN_INTERVAL = timedelta(seconds=30)


_LOGGER = logging.getLogger(__name__)

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'
EFFECT_PARSE = re.compile(r"\[(\d+)/(\d+)/(\d+)/(-?\d+)]")
SEGMENTED_MODELS = ['H6053', 'H6072', 'H6102', 'H6199', 'H70B1']

class LedCommand(IntEnum):
    """ A control command packet's type. """
    POWER = 0x01
    BRIGHTNESS = 0x04
    COLOR = 0x05


class LedMode(IntEnum):
    """
    The mode in which a color change happens in.
    
    Currently only manual is supported.
    """
    MANUAL = 0x02
    MICROPHONE = 0x06
    SCENES = 0x05
    SEGMENTS = 0x15


def _find_capability(capabilities: list, cap_type: str, instance: str) -> dict | None:
    for cap in capabilities:
        if cap.get('type') == cap_type and cap.get('instance') == instance:
            return cap
    return None


def _segment_count(capability: dict, default: int = 15) -> int:
    """Read the number of controllable segments from a segment_color_setting capability."""
    try:
        for field in capability['parameters']['fields']:
            if field.get('fieldName') == 'segment':
                return int(field['size']['max'])
    except (KeyError, TypeError, ValueError):
        pass
    return default


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        return

    if hub.devices is not None:
        devices = hub.devices
        for device in devices:
            if device['type'] == 'devices.types.light':
                _LOGGER.info("Adding device: %s", device)
                entities = [GoveeAPILight(hub, device)]
                segment_cap = _find_capability(device.get("capabilities", []),
                                               'devices.capabilities.segment_color_setting',
                                               'segmentedColorRgb')
                if segment_cap is not None:
                    count = _segment_count(segment_cap)
                    _LOGGER.info("Adding %d segments for device: %s", count, device["device"])
                    entities += [GoveeAPISegmentLight(hub, device, index) for index in range(count)]
                async_add_entities(entities)
    elif hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, hub.address.upper(), False)
        async_add_entities([GoveeBluetoothLight(hub, ble_device, config_entry)])


class GoveeAPILight(LightEntity, dict):
    _attr_color_mode = ColorMode.RGB

    def __init__(self, hub: Hub, device: dict) -> None:
        """Initialize an API light."""
        super().__init__()

        self.hub = hub

        self._state = None
        self._brightness = None

        self.device_data = device
        self.sku = self.device_data["sku"]
        self.device = self.device_data["device"]

        self._attr_name = device["deviceName"]

        color_modes: set[ColorMode] = set()
        self._has_diy_scenes = False

        for cap in device["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                color_modes.add(ColorMode.ONOFF)
            if cap['instance'] == 'brightness':
                color_modes.add(ColorMode.BRIGHTNESS)
            if cap['instance'] == 'colorTemperatureK':
                color_modes.add(ColorMode.COLOR_TEMP)
                self._attr_min_color_temp_kelvin = cap['parameters']['range']['min']
                self._attr_max_color_temp_kelvin = cap['parameters']['range']['max']
                self._attr_min_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_min_color_temp_kelvin)
                self._attr_max_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_max_color_temp_kelvin)
            if cap['instance'] == 'colorRgb':
                color_modes.add(ColorMode.RGB)
            if cap['instance'] == 'lightScene':
                self._attr_supported_features = LightEntityFeature(
                    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
                )
            if cap['instance'] == 'diyScene':
                self._has_diy_scenes = True
                self._attr_supported_features = LightEntityFeature(
                    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
                )

        if ColorMode.ONOFF in color_modes:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        if ColorMode.BRIGHTNESS in color_modes:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        if ColorMode.COLOR_TEMP in color_modes:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        if ColorMode.RGB in color_modes:
            self._attr_supported_color_modes = {ColorMode.RGB}

        self._state = None
        self._brightness = None

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self.device)},
            "name": self.device_data["deviceName"],
            "manufacturer": "Govee",
            "model": self.sku,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self.update_scenes()

    async def async_update(self):
        """Retrieve latest state."""
        _LOGGER.info("Updating device: %s", self.device_data)

        state = await self.hub.api.get_device_state(self.sku, self.device)
        for cap in state["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                self._state = cap['state']['value'] == 1
            if cap['instance'] == 'brightness':
                self._brightness = cap['state']['value']
            if cap['instance'] == 'colorTemperatureK':
                value = cap['state']['value']
                if value != 0:
                    self._attr_color_temp_kelvin = value
                    self._attr_color_temp = color_util.color_temperature_kelvin_to_mired(value)
            if cap['instance'] == 'colorRgb':
                num = cap['state']['value']
                self._attr_rgb_color = ((num >> 16) & 0xFF, (num >> 8) & 0xFF, num & 0xFF)

    async def update_scenes(self):
        if LightEntityFeature.EFFECT not in self.supported_features:
            return
        if self._attr_effect_list:
            return

        _LOGGER.info("Updating device effects: %s", self.device_data)
        scenes = []

        try:
            scenes += await self.hub.api.list_scenes(self.sku, self.device)
        except Exception:
            _LOGGER.exception("Failed to load light scenes for %s", self.sku)

        if self._has_diy_scenes:
            try:
                for scene in await self.hub.api.list_diy_scenes(self.sku, self.device):
                    scenes.append({**scene, 'name': f"DIY: {scene['name']}"})
            except Exception:
                _LOGGER.exception("Failed to load DIY scenes for %s", self.sku)

        store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
        await store.async_save(scenes)

        self._attr_effect_list = [scene['name'] for scene in scenes]
        self.async_write_ha_state()

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self.device

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            await self.hub.api.set_brightness(self.sku, self.device, (brightness / 255) * 100)
            self._brightness = brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            await self.hub.api.set_color_rgb(self.sku, self.device, red, green, blue)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            await self.hub.api.set_color_temp(self.sku, self.device, kelvin)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs.get(ATTR_EFFECT)
            store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
            scenes = (
                scene for scene in await store.async_load()
                if scene['name'] == effect_name
            )
            scene = next(scenes)
            _LOGGER.info("Set scene: %s", scene)
            instance = 'diyScene' if isinstance(scene['value'], int) else 'lightScene'
            await self.hub.api.set_scene(self.sku, self.device, scene['value'], instance)

        await self.hub.api.toggle_power(self.sku, self.device, 1)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.api.toggle_power(self.sku, self.device, 0)
        self._state = False


class GoveeAPISegmentLight(LightEntity):
    """One controllable segment of a Govee light, driven through the cloud API.

    The Govee API exposes no per-segment power or state read-back, so on/off is
    emulated with segmented brightness and the state here is optimistic.
    """

    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: Hub, device: dict, index: int) -> None:
        self.hub = hub
        self.device_data = device
        self.sku = device["sku"]
        self.device = device["device"]
        self._index = index

        self._attr_name = f"Segment {index + 1}"
        self._attr_unique_id = f"{self.device}_segment_{index}"

        self._state = None
        self._brightness = 255
        self._attr_rgb_color = (255, 255, 255)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self.device)},
            "name": self.device_data["deviceName"],
            "manufacturer": "Govee",
            "model": self.sku,
        }

    @property
    def is_on(self) -> bool | None:
        return self._state

    @property
    def brightness(self):
        return self._brightness

    async def async_turn_on(self, **kwargs) -> None:
        segments = [self._index]

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs[ATTR_RGB_COLOR]
            await self.hub.api.set_segment_rgb(self.sku, self.device, segments, red, green, blue)
            self._attr_rgb_color = (red, green, blue)

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            await self.hub.api.set_segment_brightness(self.sku, self.device, segments,
                                                      round(brightness / 255 * 100))
            self._brightness = brightness
        elif ATTR_RGB_COLOR not in kwargs:
            # Plain toggle on: re-assert the last colour so the segment lights up.
            red, green, blue = self._attr_rgb_color
            await self.hub.api.set_segment_rgb(self.sku, self.device, segments, red, green, blue)

        self._state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.api.set_segment_brightness(self.sku, self.device, [self._index], 0)
        self._state = False
        self.async_write_ha_state()


class GoveeBluetoothLight(LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature(
        LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION)

    def __init__(self, hub: Hub, ble_device, config_entry: ConfigEntry) -> None:
        """Initialize an bluetooth light."""
        self._mac = hub.address
        self._model = config_entry.data["model"]
        self._is_segmented = self._model in SEGMENTED_MODELS
        self._ble_device = ble_device
        self._state = None
        self._brightness = None

    def _load_effects_json(self) -> dict:
        return json.loads(Path(Path(__file__).parent / "jsons" / (self._model + ".json")).read_text())

    def _iter_effects(self):
        """Yield (label, categoryIdx, sceneIdx, lightEffectIdx, specialEffectIdx) for every
        applicable scene. specialEffectIdx == -1 means use the lightEffect's own scenceParam.

        Govee ships each scene either as a base payload on the lightEffect (`scenceParam`)
        or as per-SKU variants under `specialEffect`. Older code only read `specialEffect`,
        which for many models is empty or tagged for other SKUs, so most scenes never showed.
        """
        json_data = self._load_effects_json()
        for categoryIdx, category in enumerate(json_data['data']['categories']):
            for sceneIdx, scene in enumerate(category['scenes']):
                for leffectIdx, lightEffect in enumerate(scene['lightEffects']):
                    specials = lightEffect.get('specialEffect') or []
                    matched = [
                        idx for idx, se in enumerate(specials)
                        if se.get('scenceParam') and self._model in se.get('supportSku', [])
                    ]
                    if matched:
                        seffectIdxs = matched
                    elif lightEffect.get('scenceParam'):
                        seffectIdxs = [-1]
                    else:
                        seffectIdxs = [idx for idx, se in enumerate(specials) if se.get('scenceParam')][:1]

                    for seffectIdx in seffectIdxs:
                        name = lightEffect.get('scenceName') or scene['sceneName']
                        indexes = f"{categoryIdx}/{sceneIdx}/{leffectIdx}/{seffectIdx}"
                        label = f"{category['categoryName']} - {scene['sceneName']} - {name} [{indexes}]"
                        yield label, categoryIdx, sceneIdx, leffectIdx, seffectIdx

    def _resolve_effect_param(self, categoryIdx: int, sceneIdx: int, leffectIdx: int, seffectIdx: int) -> str:
        lightEffect = self._load_effects_json()['data']['categories'][categoryIdx]['scenes'][sceneIdx]['lightEffects'][
            leffectIdx]
        if seffectIdx < 0:
            return lightEffect['scenceParam']
        return lightEffect['specialEffect'][seffectIdx]['scenceParam']

    @property
    def effect_list(self) -> list[str] | None:
        return [label for label, *_ in self._iter_effects()]

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return "GOVEE Light"

    @property
    def unique_id(self) -> str:
        """Return a unique, Home Assistant friendly identifier for this entity."""
        return self._mac.replace(":", "")

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        commands = [self._prepareSinglePacketData(LedCommand.POWER, [0x1])]

        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [brightness]))
            self._brightness = brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            if self._is_segmented:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR,
                                                              [LedMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00, 0x00,
                                                               0x00, 0x00, 0xFF, 0x7F]))
            else:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR, [LedMode.MANUAL, red, green, blue]))
        if ATTR_EFFECT in kwargs:
            effect = kwargs.get(ATTR_EFFECT)
            if len(effect) > 0:
                search = EFFECT_PARSE.search(effect)

                # Parse effect indexes
                categoryIndex = int(search.group(1))
                sceneIndex = int(search.group(2))
                lightEffectIndex = int(search.group(3))
                specialEffectIndex = int(search.group(4))

                scene_param = self._resolve_effect_param(categoryIndex, sceneIndex, lightEffectIndex,
                                                         specialEffectIndex)

                # Prepare packets to send big payload in separated chunks
                for command in prepareMultiplePacketsData(0xa3,
                                                          array.array('B', [0x02]),
                                                          array.array('B',
                                                                      base64.b64decode(scene_param)
                                                                      )):
                    commands.append(command)

        for command in commands:
            client = await self._connectBluetooth()
            await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, command, False)

    async def async_turn_off(self, **kwargs) -> None:
        client = await self._connectBluetooth()
        await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC,
                                     self._prepareSinglePacketData(LedCommand.POWER, [0x0]), False)
        self._state = False

    async def _connectBluetooth(self) -> BleakClient:
        for i in range(3):
            try:
                client = await bleak_retry_connector.establish_connection(BleakClient, self._ble_device, self.unique_id)
                return client
            except:
                continue

    def _prepareSinglePacketData(self, cmd, payload):
        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (
                isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        cmd = cmd & 0xFF
        payload = bytes(payload)

        frame = bytes([0x33, cmd]) + bytes(payload)
        # pad frame data to 19 bytes (plus checksum)
        frame += bytes([0] * (19 - len(frame)))

        # The checksum is calculated by XORing all data bytes
        checksum = 0
        for b in frame:
            checksum ^= b

        frame += bytes([checksum & 0xFF])
        return frame
