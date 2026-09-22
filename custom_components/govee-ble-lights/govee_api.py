import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession


class GoveeAPI:
    def __init__(self, hass: HomeAssistant, api_key: str):
        self._session = async_get_clientsession(hass)
        self.api_key = api_key
        self.base_url = "https://openapi.api.govee.com/router/api/v1"
        self.headers = {
            "Govee-API-Key": self.api_key,
            "Content-Type": "application/json"
        }

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        async with self._session.request(method, url, headers=self.headers, json=payload) as response:
            response.raise_for_status()
            return await response.json()

    async def _control(self, sku: str, device: str, capability: dict) -> dict:
        return await self._request('POST', '/device/control', {
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device,
                'capability': capability,
            }
        })

    async def list_devices(self):
        data = await self._request('GET', '/user/devices')
        return data['data']

    async def list_scenes(self, sku: str, device: str):
        data = await self._request('POST', '/device/scenes', {
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device,
            }
        })
        return self._scene_options(data)

    async def list_diy_scenes(self, sku: str, device: str):
        data = await self._request('POST', '/device/diy-scenes', {
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device,
            }
        })
        return self._scene_options(data)

    @staticmethod
    def _scene_options(data: dict) -> list:
        """Pull the scene option list out of a scenes/diy-scenes response.

        A device with no scenes configured for that catalog (common for DIY
        scenes) omits `capabilities` or its `parameters`/`options` entirely,
        rather than returning an empty options list - treat that as "no
        scenes" instead of an error.
        """
        capabilities = data.get('payload', {}).get('capabilities') or []
        if not capabilities:
            return []
        return capabilities[0].get('parameters', {}).get('options', [])

    async def toggle_power(self, sku: str, device: str, value: int):
        return await self._control(sku, device, {
            'type': 'devices.capabilities.on_off',
            'instance': 'powerSwitch',
            'value': value
        })

    async def get_device_state(self, sku: str, device: str):
        data = await self._request('POST', '/device/state', {
            'requestId': uuid.uuid4().hex,
            'payload': {
                'sku': sku,
                'device': device
            }
        })
        return data['payload']

    async def set_color_rgb(self, sku: str, device: str, r: int, g: int, b: int):
        return await self._control(sku, device, {
            'type': 'devices.capabilities.color_setting',
            'instance': 'colorRgb',
            'value': ((r & 0xFF) << 16) | ((g & 0xFF) << 8) | ((b & 0xFF) << 0)
        })

    async def set_color_temp(self, sku: str, device: str, kelvin: int):
        return await self._control(sku, device, {
            'type': 'devices.capabilities.color_setting',
            'instance': 'colorTemperatureK',
            'value': kelvin
        })

    async def set_brightness(self, sku: str, device: str, value: int):
        return await self._control(sku, device, {
            'type': 'devices.capabilities.range',
            'instance': 'brightness',
            'value': value
        })

    async def set_scene(self, sku: str, device: str, value: object, instance: str = 'lightScene'):
        return await self._control(sku, device, {
            'type': 'devices.capabilities.dynamic_scene',
            'instance': instance,
            'value': value
        })

    async def set_segment_rgb(self, sku: str, device: str, segments: list, r: int, g: int, b: int):
        return await self._control(sku, device, {
            'type': 'devices.capabilities.segment_color_setting',
            'instance': 'segmentedColorRgb',
            'value': {
                'segment': segments,
                'rgb': ((r & 0xFF) << 16) | ((g & 0xFF) << 8) | ((b & 0xFF) << 0)
            }
        })

    async def set_segment_brightness(self, sku: str, device: str, segments: list, value: int):
        return await self._control(sku, device, {
            'type': 'devices.capabilities.segment_color_setting',
            'instance': 'segmentedBrightness',
            'value': {
                'segment': segments,
                'brightness': value
            }
        })
