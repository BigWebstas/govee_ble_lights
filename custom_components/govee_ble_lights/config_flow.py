from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import (CONF_ADDRESS, CONF_MODEL, CONF_API_KEY, CONF_TYPE)
from homeassistant.data_entry_flow import FlowResult

from govee_local_api import GoveeController

from .const import DOMAIN, CONF_TYPE_API, CONF_TYPE_BLE, CONF_TYPE_LAN, CONF_FINGERPRINT
from pathlib import Path
import asyncio

class GoveeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._api_key: str = ''
        self._config_type: str = ''
        self._discovery_info: None = None
        self._discovered_device: None = None
        self._discovered_devices: dict[str, str] = {}
        self._available_models: list[str] = []
        self._available_config_types: dict[str, str] = {
            CONF_TYPE_API: 'API',
            CONF_TYPE_BLE: 'BLE',
            CONF_TYPE_LAN: 'LAN',
        }
        self._discovered_lan_devices: dict[str, tuple[str, str]] = {}

        jsons_path = Path(Path(__file__).parent / "jsons")
        for file in jsons_path.iterdir():
            self._available_models.append(file.name.replace(".json", ""))

        self._available_models.sort()

    async def async_step_bluetooth(
            self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        assert self._discovery_info is not None
        discovery_info = self._discovery_info
        title = discovery_info.name
        if user_input is not None:
            model = user_input[CONF_MODEL]
            return self.async_create_entry(title=title, data={
                CONF_MODEL: model
            })

        self._set_confirm_only()
        placeholders = {
            "name": title,
            "model": "Device model"
        }
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=placeholders,
            data_schema=vol.Schema({
                vol.Required(CONF_MODEL): vol.In(self._available_models)
            }),
        )

    async def async_step_api(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors = {}

        if user_input is not None and CONF_API_KEY in user_input and user_input[CONF_API_KEY] is not None:
            api_key = user_input[CONF_API_KEY]
            return self.async_create_entry(
                title='Govee API',
                data={
                    CONF_API_KEY: api_key
                }
            )

        return self.async_show_form(
            step_id="api",
            data_schema=vol.Schema({
                vol.Required(CONF_API_KEY): str
            }),
            errors=errors
        )

    async def async_step_ble(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors = {}
        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            self._discovered_devices[address] = (discovery_info.name)

        if (user_input is not None and CONF_ADDRESS in user_input and user_input[CONF_ADDRESS] is not None
                and CONF_MODEL in user_input and user_input[CONF_MODEL] is not None):
            address = user_input[CONF_ADDRESS]
            model = user_input[CONF_MODEL]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._discovered_devices[address], data={
                    CONF_MODEL: model
                }
            )

        return self.async_show_form(
            step_id="ble",
            data_schema=vol.Schema({
                vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices),
                vol.Required(CONF_MODEL): vol.In(self._available_models)
            }),
            errors=errors
        )

    async def async_step_lan(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors = {}

        if not self._discovered_lan_devices:
            def _discovered(device, is_new: bool) -> bool:
                self._discovered_lan_devices[device.fingerprint] = (device.ip, device.sku)
                return True

            controller = GoveeController(discovery_enabled=True, discovered_callback=_discovered)
            await controller.start()
            for _ in range(10):
                if self._discovered_lan_devices:
                    break
                await asyncio.sleep(0.5)
            cleanup_done = controller.cleanup()
            await cleanup_done.wait()

        current_ids = self._async_current_ids()
        available = {
            fingerprint: f"{sku} ({ip})"
            for fingerprint, (ip, sku) in self._discovered_lan_devices.items()
            if fingerprint not in current_ids
        }

        if not available:
            errors["base"] = "no_devices_found"

        if user_input is not None and CONF_ADDRESS in user_input and user_input[CONF_ADDRESS] is not None:
            fingerprint = user_input[CONF_ADDRESS]
            ip, sku = self._discovered_lan_devices[fingerprint]
            await self.async_set_unique_id(fingerprint, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"{sku} ({ip})", data={
                    CONF_ADDRESS: ip,
                    CONF_MODEL: sku,
                    CONF_FINGERPRINT: fingerprint,
                }
            )

        return self.async_show_form(
            step_id="lan",
            data_schema=vol.Schema({
                vol.Required(CONF_ADDRESS): vol.In(available),
            }),
            errors=errors
        )

    async def async_step_user(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None and user_input[CONF_TYPE] == CONF_TYPE_API:
            return await self.async_step_api(user_input)
        if user_input is not None and user_input[CONF_TYPE] == CONF_TYPE_BLE:
            return await self.async_step_ble(user_input)
        if user_input is not None and user_input[CONF_TYPE] == CONF_TYPE_LAN:
            return await self.async_step_lan(user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_TYPE): vol.In(self._available_config_types),
            }),
        )
