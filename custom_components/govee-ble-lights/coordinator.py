from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .govee_api import GoveeAPI

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)


class GoveeStateCoordinator(DataUpdateCoordinator[dict]):
    """Polls cloud device state for every cloud-polled Govee light in one shared cycle.

    Govee's OpenAPI has no bulk state endpoint, so this doesn't reduce the
    number of HTTP calls - it centralizes polling and gives cloud-polled
    entities uniform availability instead of each entity polling and
    failing independently. Entities register interest via `context` (their
    cloud device id) when constructed as a `CoordinatorEntity`; a device
    with no entity currently interested in cloud state (e.g. a LAN-backed
    hybrid light, which gets pushed state instead) is simply never fetched.
    """

    def __init__(self, hass: HomeAssistant, api: GoveeAPI, sku_by_device: dict[str, str]) -> None:
        super().__init__(hass, _LOGGER, name="Govee cloud state", update_interval=SCAN_INTERVAL)
        self._api = api
        self._sku_by_device = sku_by_device

    async def _async_update_data(self) -> dict[str, dict | None]:
        device_ids = [device_id for device_id in self.async_contexts() if device_id is not None]
        results = await asyncio.gather(
            *(self._api.get_device_state(self._sku_by_device[device_id], device_id) for device_id in device_ids),
            return_exceptions=True,
        )

        data = {}
        for device_id, result in zip(device_ids, results):
            if isinstance(result, Exception):
                _LOGGER.warning("Failed to fetch state for %s: %s", device_id, result)
                data[device_id] = None
            else:
                data[device_id] = result
        return data
