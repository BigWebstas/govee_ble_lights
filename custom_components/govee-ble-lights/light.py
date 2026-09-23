from __future__ import annotations

import array
import logging

from enum import IntEnum
import bleak_retry_connector

from bleak import BleakClient
from govee_local_api import GoveeDevice
from homeassistant.components import bluetooth
from homeassistant.components.light import (ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_EFFECT, ColorMode, LightEntity,
                                            LightEntityFeature, ATTR_COLOR_TEMP_KELVIN)

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import homeassistant.util.color as color_util

from .const import DOMAIN
from pathlib import Path
import json
from .coordinator import SCAN_INTERVAL
from .govee_utils import prepareMultiplePacketsData
import base64
import homeassistant.util.dt as dt_util
from . import Hub

_LOGGER = logging.getLogger(__name__)

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'
SEGMENTED_MODELS = ['H6053', 'H6072', 'H6102', 'H6199', 'H70B1', 'H6095', 'H7092', 'H619D']

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


async def _load_cloud_scenes(hass: HomeAssistant, hub: Hub, sku: str, device: str, has_diy_scenes: bool) -> list[dict]:
    """Fetch the cloud scene/DIY-scene catalog for a device and cache it for later lookup."""
    scenes = []

    try:
        scenes += await hub.api.list_scenes(sku, device)
    except Exception:
        _LOGGER.exception("Failed to load light scenes for %s", sku)

    if has_diy_scenes:
        try:
            for scene in await hub.api.list_diy_scenes(sku, device):
                scenes.append({**scene, 'name': f"DIY: {scene['name']}"})
        except Exception:
            _LOGGER.exception("Failed to load DIY scenes for %s", sku)

    store = Store(hass, 1, f"{DOMAIN}/effect_list_{sku}.json")
    await store.async_save(scenes)
    return scenes


async def _resolve_cloud_scene(hass: HomeAssistant, sku: str, effect_name: str) -> dict:
    """Look up a previously cached cloud scene by its display name."""
    store = Store(hass, 1, f"{DOMAIN}/effect_list_{sku}.json")
    scenes = await store.async_load()
    return next(scene for scene in scenes if scene['name'] == effect_name)


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

                light_entity = GoveeAPILight(hub, device)

                lan_device = None
                if hub.lan_controller is not None:
                    lan_device = next(
                        (d for d in hub.lan_controller.devices if d.fingerprint == device["device"]), None
                    )
                    if lan_device is not None:
                        _LOGGER.info("Cloud device %s matched LAN device at %s",
                                     device["device"], lan_device.ip)

                if lan_device is not None:
                    light_entity = GoveeHybridLight(hub, device, lan_device=lan_device)

                entities = [light_entity]
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
    elif hub.lan_controller is not None:
        async_add_entities([GoveeLANLight(device) for device in hub.lan_controller.devices])


class GoveeAPILight(CoordinatorEntity, LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_should_poll = False

    def __init__(self, hub: Hub, device: dict) -> None:
        """Initialize an API light."""
        super().__init__(hub.coordinator, context=device["device"])

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
        await self.coordinator.async_request_refresh()
        await self.update_scenes()

    @property
    def available(self) -> bool:
        return super().available and (self.coordinator.data or {}).get(self.device) is not None

    def _handle_coordinator_update(self) -> None:
        state = (self.coordinator.data or {}).get(self.device)
        if state is not None:
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
        super()._handle_coordinator_update()

    async def update_scenes(self):
        if LightEntityFeature.EFFECT not in self.supported_features:
            return
        if self._attr_effect_list:
            return

        _LOGGER.info("Updating device effects: %s", self.device_data)
        scenes = await _load_cloud_scenes(self.hass, self.hub, self.sku, self.device, self._has_diy_scenes)
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
            scene = await _resolve_cloud_scene(self.hass, self.sku, effect_name)
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


class GoveeLANLight(LightEntity):
    """A Govee light controlled over the local network (LAN UDP API).

    State is pushed by the govee-local-api controller's own status-poll and
    discovery loop, so this entity doesn't poll on its own.
    """

    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_should_poll = False

    def __init__(self, device: GoveeDevice) -> None:
        self._device = device
        self._attr_name = "GOVEE Light"
        self._attr_unique_id = device.fingerprint

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._device.fingerprint)},
            "name": "GOVEE Light",
            "manufacturer": "Govee",
            "model": self._device.sku,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._device.set_update_callback(lambda device: self.async_write_ha_state())

    async def async_will_remove_from_hass(self) -> None:
        self._device.set_update_callback(None)
        await super().async_will_remove_from_hass()

    @property
    def available(self) -> bool:
        # getattr guard: is_connected was added in govee-local-api 3.0.0; an
        # older cached install (unpinned before this integration required
        # >=3.0.0) would otherwise crash every state update.
        return getattr(self._device, "is_connected", True)

    @property
    def is_on(self) -> bool | None:
        return self._device.on

    @property
    def brightness(self):
        return round(self._device.brightness / 100 * 255)

    @property
    def rgb_color(self):
        return self._device.rgb_color

    async def async_turn_on(self, **kwargs) -> None:
        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs[ATTR_RGB_COLOR]
            await self._device.set_rgb_color(red, green, blue)

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            await self._device.set_brightness(round(brightness / 255 * 100))

        await self._device.turn_on()

    async def async_turn_off(self, **kwargs) -> None:
        await self._device.turn_off()


class GoveeBLEControlMixin:
    """Shared BLE packet-building and local effect-catalog logic.

    Expects the including class to set `self._model`, `self._is_segmented`
    and `self._ble_device`, and to implement a `unique_id` property (used as
    the connection cache key).
    """

    def _load_effects_json(self) -> dict:
        return json.loads(Path(Path(__file__).parent / "jsons" / (self._model + ".json")).read_text())

    def _iter_effects(self):
        """Yield (name, categoryName, categoryIdx, sceneIdx, lightEffectIdx, specialEffectIdx)
        for every applicable scene. specialEffectIdx == -1 means use the lightEffect's own
        scenceParam.

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
                        yield name, category['categoryName'], categoryIdx, sceneIdx, leffectIdx, seffectIdx

    def _effect_map(self) -> dict[str, tuple[int, int, int, int]]:
        """Map each displayed effect name to its (categoryIdx, sceneIdx, lightEffectIdx,
        specialEffectIdx).

        Displayed names match the plain scene names cloud-API devices show (e.g. "Fall")
        instead of the raw category/index-suffixed form, since BLE devices have no cloud
        catalog to resolve a selection by cloud scene id - the name itself has to double as
        the lookup key. Govee reuses scene names across (and even within) categories, so a
        name is disambiguated with its category in parentheses when it collides with
        another, and with a counter on top of that for same-name-same-category scenes.
        """
        entries = list(self._iter_effects())
        counts: dict[str, int] = {}
        for name, *_ in entries:
            counts[name] = counts.get(name, 0) + 1

        mapping: dict[str, tuple[int, int, int, int]] = {}
        for name, category_name, categoryIdx, sceneIdx, leffectIdx, seffectIdx in entries:
            label = f"{name} ({category_name})" if counts[name] > 1 else name
            if label in mapping:
                suffix = 2
                while f"{label} #{suffix}" in mapping:
                    suffix += 1
                label = f"{label} #{suffix}"
            mapping[label] = (categoryIdx, sceneIdx, leffectIdx, seffectIdx)
        return mapping

    def _resolve_effect_param(self, categoryIdx: int, sceneIdx: int, leffectIdx: int, seffectIdx: int) -> str:
        lightEffect = self._load_effects_json()['data']['categories'][categoryIdx]['scenes'][sceneIdx]['lightEffects'][
            leffectIdx]
        if seffectIdx < 0:
            return lightEffect['scenceParam']
        return lightEffect['specialEffect'][seffectIdx]['scenceParam']

    def _local_ble_effect_list(self) -> list[str]:
        return list(self._effect_map().keys())

    def _build_ble_on_commands(self, **kwargs) -> list[bytes]:
        commands = [self._prepareSinglePacketData(LedCommand.POWER, [0x1])]

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [brightness]))

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
                categoryIndex, sceneIndex, lightEffectIndex, specialEffectIndex = self._effect_map()[effect]

                scene_param = self._resolve_effect_param(categoryIndex, sceneIndex, lightEffectIndex,
                                                         specialEffectIndex)

                # Prepare packets to send big payload in separated chunks
                for command in prepareMultiplePacketsData(0xa3,
                                                          array.array('B', [0x02]),
                                                          array.array('B',
                                                                      base64.b64decode(scene_param)
                                                                      )):
                    commands.append(command)

        return commands

    def _build_ble_off_command(self) -> bytes:
        return self._prepareSinglePacketData(LedCommand.POWER, [0x0])

    async def _write_ble_commands(self, commands: list[bytes]) -> None:
        client = await self._connectBluetooth()
        for command in commands:
            await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, command, False)

    async def _connectBluetooth(self) -> BleakClient:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                return await bleak_retry_connector.establish_connection(BleakClient, self._ble_device, self.unique_id)
            except Exception as ex:
                last_error = ex
                continue
        raise ConnectionError(f"Could not connect to {self.unique_id} after 3 attempts") from last_error

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


class GoveeBluetoothLight(LightEntity, GoveeBLEControlMixin):
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

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._mac.replace(":", ""))},
            "name": "GOVEE Light",
            "manufacturer": "Govee",
            "model": self._model,
        }

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

    @property
    def effect_list(self) -> list[str] | None:
        return self._local_ble_effect_list()

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs.get(ATTR_BRIGHTNESS, 255)

        await self._write_ble_commands(self._build_ble_on_commands(**kwargs))

    async def async_turn_off(self, **kwargs) -> None:
        await self._write_ble_commands([self._build_ble_off_command()])
        self._state = False


class GoveeHybridLight(CoordinatorEntity, LightEntity):
    """A cloud-API device that's also visible over LAN.

    Commands are tried LAN first, falling through to the cloud API on any
    error so the light still responds. State for a LAN-backed device is
    pushed live by the LAN controller.

    Effects: a LAN-matched device uses the cloud's scene catalog, so a
    failed LAN `set_scene` can fall back to the cloud's own `set_scene`
    call with the same value.
    """

    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_should_poll = False

    def __init__(self, hub: Hub, device: dict, lan_device=None) -> None:
        super().__init__(hub.coordinator, context=device["device"] if lan_device is None else None)

        self.hub = hub
        self.device_data = device
        self.sku = device["sku"]
        self.device = device["device"]
        self._lan_device = lan_device
        self._last_lan_check = dt_util.utcnow()
        self._cloud_listener_unsub = None

        self._attr_name = device["deviceName"]
        self._attr_effect_list = None
        self._has_diy_scenes = False

        supports_effect = False
        if lan_device is not None:
            for cap in device["capabilities"]:
                if cap['instance'] == 'lightScene':
                    supports_effect = True
                if cap['instance'] == 'diyScene':
                    supports_effect = True
                    self._has_diy_scenes = True

        self._attr_supported_features = LightEntityFeature(
            (LightEntityFeature.EFFECT if supports_effect else LightEntityFeature(0))
            | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
        )

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

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self.device

    @property
    def available(self) -> bool:
        if self._lan_device is not None:
            # getattr guard: is_connected was added in govee-local-api 3.0.0;
            # an older cached install would otherwise crash every update.
            return getattr(self._lan_device, "is_connected", True)
        return super().available and (self.coordinator.data or {}).get(self.device) is not None

    @property
    def brightness(self):
        if self._lan_device is not None:
            return round(self._lan_device.brightness / 100 * 255)
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        if self._lan_device is not None:
            return self._lan_device.on
        return self._state

    @property
    def rgb_color(self):
        if self._lan_device is not None:
            return self._lan_device.rgb_color
        return self._attr_rgb_color

    @property
    def effect_list(self) -> list[str] | None:
        if self._lan_device is not None:
            return self._attr_effect_list
        return None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._lan_device is not None:
            self._lan_device.set_update_callback(lambda device: self.async_write_ha_state())
            await self._update_cloud_scenes()
        else:
            await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self._lan_device is not None:
            self._lan_device.set_update_callback(None)
        await super().async_will_remove_from_hass()

    async def _update_cloud_scenes(self) -> None:
        if LightEntityFeature.EFFECT not in self.supported_features:
            return
        if self._attr_effect_list:
            return

        scenes = await _load_cloud_scenes(self.hass, self.hub, self.sku, self.device, self._has_diy_scenes)
        self._attr_effect_list = [scene['name'] for scene in scenes]
        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        """Absorb cloud-polled state (used when not LAN-backed; BLE has no status read-back)."""
        if self._lan_device is None:
            state = (self.coordinator.data or {}).get(self.device)
            if state is not None:
                for cap in state["capabilities"]:
                    if cap['instance'] == 'powerSwitch':
                        self._state = cap['state']['value'] == 1
                    if cap['instance'] == 'brightness':
                        self._brightness = cap['state']['value']
                    if cap['instance'] == 'colorRgb':
                        num = cap['state']['value']
                        self._attr_rgb_color = ((num >> 16) & 0xFF, (num >> 8) & 0xFF, num & 0xFF)
        super()._handle_coordinator_update()

    def _refresh_lan_device(self) -> None:
        """Re-resolve the matched LAN device by fingerprint, throttled to SCAN_INTERVAL.

        `hub.lan_controller.devices` can evict and re-add a device (e.g. after an
        IP change or a dropout), replacing the object this entity matched at
        startup. Without this, a stale `_lan_device` would keep failing forever
        even once the device is reachable again under a fresh object.
        """
        if self.hub.lan_controller is None:
            return
        now = dt_util.utcnow()
        if now - self._last_lan_check < SCAN_INTERVAL:
            return
        self._last_lan_check = now
        self._lan_device = next(
            (d for d in self.hub.lan_controller.devices if d.fingerprint == self.device), None
        )
        if self._lan_device is None:
            self._ensure_cloud_polling()

    def _ensure_cloud_polling(self) -> None:
        """Register for cloud-state polling once the LAN match is lost.

        This entity is set up with no coordinator context (it's normally
        LAN-pushed), so the shared coordinator never fetches its cloud state.
        If `_refresh_lan_device` demotes it to cloud-only, nothing would ever
        populate `is_on`/`brightness`/`rgb_color` or clear `available` without
        this - it would look permanently unavailable even though the cloud
        fallback in `async_turn_on`/`async_turn_off` still works.
        """
        if self._cloud_listener_unsub is not None:
            return
        self._cloud_listener_unsub = self.coordinator.async_add_listener(
            self._handle_coordinator_update, self.device
        )
        self.async_on_remove(self._cloud_listener_unsub)
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def async_turn_on(self, **kwargs) -> None:
        self._refresh_lan_device()
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs.get(ATTR_BRIGHTNESS, 255)

        if ATTR_EFFECT in kwargs and self._lan_device is not None:
            effect_name = kwargs[ATTR_EFFECT]
            try:
                scene = await _resolve_cloud_scene(self.hass, self.sku, effect_name)
                await self._lan_device.set_scene(str(scene['value']))
                return
            except Exception:
                _LOGGER.warning("LAN effect failed for %s, falling back to cloud API",
                                self.device, exc_info=True)
                try:
                    scene = await _resolve_cloud_scene(self.hass, self.sku, effect_name)
                    instance = 'diyScene' if isinstance(scene['value'], int) else 'lightScene'
                    await self.hub.api.set_scene(self.sku, self.device, scene['value'], instance)
                except Exception:
                    _LOGGER.exception("Cloud fallback for effect also failed for %s", self.device)
                return

        if self._lan_device is not None and ATTR_EFFECT not in kwargs:
            try:
                if ATTR_RGB_COLOR in kwargs:
                    red, green, blue = kwargs[ATTR_RGB_COLOR]
                    await self._lan_device.set_rgb_color(red, green, blue)
                if ATTR_BRIGHTNESS in kwargs:
                    await self._lan_device.set_brightness(round(self._brightness / 255 * 100))
                await self._lan_device.turn_on()
                return
            except Exception:
                _LOGGER.warning("LAN control failed for %s, falling back", self.device, exc_info=True)

        if ATTR_EFFECT in kwargs and self._lan_device is None:
            _LOGGER.warning("No LAN match for %s; can't apply effect locally or via cloud", self.device)
            return

        if ATTR_BRIGHTNESS in kwargs:
            await self.hub.api.set_brightness(self.sku, self.device, (self._brightness / 255) * 100)

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            await self.hub.api.set_color_rgb(self.sku, self.device, red, green, blue)

        await self.hub.api.toggle_power(self.sku, self.device, 1)

    async def async_turn_off(self, **kwargs) -> None:
        self._refresh_lan_device()
        if self._lan_device is not None:
            try:
                await self._lan_device.turn_off()
                self._state = False
                return
            except Exception:
                _LOGGER.warning("LAN control failed for %s, falling back", self.device, exc_info=True)

        await self.hub.api.toggle_power(self.sku, self.device, 0)
        self._state = False
