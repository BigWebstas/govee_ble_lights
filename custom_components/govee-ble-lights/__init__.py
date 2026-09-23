from __future__ import annotations

import asyncio

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import (CONF_ADDRESS, CONF_API_KEY, CONF_MODEL, MAJOR_VERSION, MINOR_VERSION)
from homeassistant.helpers.storage import Store

from govee_local_api import GoveeController

from .coordinator import GoveeStateCoordinator
from .govee_api import GoveeAPI

from .const import DOMAIN
import logging

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["light", "sensor"]

LAN_CONTROLLER_KEY = "_lan_controller"
LAN_CONTROLLER_REFS_KEY = "_lan_controller_refs"


class Hub:
    def __init__(self, api: GoveeAPI | None, address: str = None, devices: list = None,
                 lan_controller: GoveeController | None = None,
                 coordinator: GoveeStateCoordinator | None = None) -> None:
        """Init Govee dummy hub."""
        self.api = api
        self.devices = devices
        self.address = address
        self.lan_controller = lan_controller
        self.coordinator = coordinator


async def _get_shared_lan_controller(hass: HomeAssistant) -> GoveeController:
    """Get or create the single GoveeController shared by every entry that needs LAN discovery.

    govee_local_api doesn't set SO_REUSEPORT on its UDP sockets, so two
    independent controllers binding the same ports (4001/4002/4003) would
    fail with "Address already in use" - every LAN-aware entry (standalone
    LAN entries and the API hub's hybrid matching) must share one instance.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if LAN_CONTROLLER_KEY not in domain_data:
        controller = GoveeController(
            loop=hass.loop,
            discovery_enabled=True,
            evict_enabled=True,
            update_enabled=True,
        )
        await controller.start()
        domain_data[LAN_CONTROLLER_KEY] = controller
        domain_data[LAN_CONTROLLER_REFS_KEY] = 0

    domain_data[LAN_CONTROLLER_REFS_KEY] += 1
    return domain_data[LAN_CONTROLLER_KEY]


async def _release_shared_lan_controller(hass: HomeAssistant) -> None:
    domain_data = hass.data.get(DOMAIN, {})
    if LAN_CONTROLLER_KEY not in domain_data:
        return

    domain_data[LAN_CONTROLLER_REFS_KEY] -= 1
    if domain_data[LAN_CONTROLLER_REFS_KEY] <= 0:
        controller = domain_data.pop(LAN_CONTROLLER_KEY)
        domain_data.pop(LAN_CONTROLLER_REFS_KEY, None)
        cleanup_done = controller.cleanup()
        await cleanup_done.wait()


async def async_setup_api(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up Govee API"""
    assert config_entry.data.get(CONF_API_KEY) is not None
    hass.async_create_task(internal_api_setup(hass, config_entry))


async def internal_api_setup(hass: HomeAssistant, entry: ConfigEntry):
    api_key = entry.data.get(CONF_API_KEY)
    api = GoveeAPI(hass, api_key)

    devices = await api.list_devices()
    _LOGGER.debug(f"Govee devices: %s", devices)

    store = Store(hass, 1, f"{DOMAIN}/{api_key}.json")
    await store.async_save(devices)
    await internal_cache_setup(hass, api, entry, devices)


UNIQUE_DEVICES = {}


async def internal_cache_setup(
        hass: HomeAssistant, api: GoveeAPI, entry: ConfigEntry, devices: list = None
):
    if devices is None:
        store = Store(hass, 1, f"{DOMAIN}/{entry.data.get(CONF_API_KEY)}.json")
        devices = await store.async_load()
        if devices:
            _LOGGER.debug(f"{len(devices)} devices loaded from cache!")

    # Broadcast-discover LAN devices so API devices that are also LAN-reachable
    # can be upgraded to hybrid control in light.py.
    lan_controller = await _get_shared_lan_controller(hass)
    await asyncio.sleep(5)

    sku_by_device = {
        device["device"]: device["sku"]
        for device in (devices or [])
        if device.get("type") == "devices.types.light"
    }
    coordinator = GoveeStateCoordinator(hass, api, sku_by_device)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = Hub(
        api, devices=devices, lan_controller=lan_controller, coordinator=coordinator
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


def internal_unique_devices(uid: str, devices: list) -> list:
    """For support multiple integrations - bind each device to one integraion.
    To avoid duplicates.
    """
    return [
        device
        for device in devices
        if UNIQUE_DEVICES.setdefault(device["device"], uid) == uid
    ]


async def async_setup_ble(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee BLE"""
    address = entry.unique_id
    assert address is not None
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Govee BLE device with address {address}"
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = Hub(None, address=address)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_setup_lan(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee LAN (UDP) device."""
    ip = entry.data.get(CONF_ADDRESS)
    assert ip is not None

    controller = await _get_shared_lan_controller(hass)
    controller.add_device_to_discovery_queue(ip)

    for _ in range(10):
        if any(device.ip == ip for device in controller.devices):
            break
        await asyncio.sleep(0.5)
    else:
        await _release_shared_lan_controller(hass)
        raise ConfigEntryNotReady(f"Could not find Govee LAN device at {ip}")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = Hub(None, lan_controller=controller)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee BLE device from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    if entry.data.get(CONF_API_KEY):
        await async_setup_api(hass, entry)
    elif entry.data.get(CONF_ADDRESS) and entry.data.get(CONF_MODEL):
        await async_setup_lan(hass, entry)
    elif entry.data.get(CONF_MODEL):
        await async_setup_ble(hass, entry)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hub: Hub = hass.data[DOMAIN].pop(entry.entry_id)
        if hub.lan_controller is not None:
            await _release_shared_lan_controller(hass)

    return unload_ok


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if (MAJOR_VERSION, MINOR_VERSION) < (2025, 7):
        raise Exception("unsupported hass version, need at least 2025.7")

    # init storage for registries
    hass.data[DOMAIN] = {}
    return True
