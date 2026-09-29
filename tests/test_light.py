from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.light import ATTR_EFFECT

from custom_components.govee_ble_lights import light
from custom_components.govee_ble_lights.light import GoveeBLEControlMixin, GoveeHybridLight

DEVICE = {
    "sku": "H6095",
    "device": "AA:BB:CC:DD:EE:FF:00:11",
    "deviceName": "Bedroom",
    "type": "devices.types.light",
    "capabilities": [
        {"instance": "powerSwitch"},
        {"instance": "lightScene"},
        {"instance": "diyScene"},
    ],
}


def _hybrid(lan_device):
    hub = MagicMock()
    hub.api = AsyncMock()
    entity = GoveeHybridLight(hub, DEVICE, lan_device=MagicMock())
    entity._lan_device = lan_device
    return entity


@pytest.mark.parametrize(
    ("value", "instance"),
    [({"id": 1234, "paramId": 5678}, "lightScene"), (42, "diyScene")],
)
async def test_hybrid_effect_goes_through_cloud(value, instance):
    lan_device = AsyncMock()
    entity = _hybrid(lan_device)
    scene = {"name": "Aurora", "value": value}

    with patch.object(light, "_resolve_cloud_scene", AsyncMock(return_value=scene)):
        await entity.async_turn_on(**{ATTR_EFFECT: "Aurora"})

    entity.hub.api.set_scene.assert_awaited_once_with(DEVICE["sku"], DEVICE["device"], value, instance)
    lan_device.set_scene.assert_not_called()


async def test_hybrid_effect_still_works_after_lan_lost():
    entity = _hybrid(None)
    scene = {"name": "Aurora", "value": {"id": 1, "paramId": 2}}

    with patch.object(light, "_resolve_cloud_scene", AsyncMock(return_value=scene)):
        await entity.async_turn_on(**{ATTR_EFFECT: "Aurora"})

    entity.hub.api.set_scene.assert_awaited_once()


def test_hybrid_effect_list_kept_after_lan_lost():
    entity = _hybrid(None)
    entity._attr_effect_list = ["Aurora"]
    assert entity.effect_list == ["Aurora"]


class _Catalog(GoveeBLEControlMixin):
    def __init__(self, model, catalog=None):
        self._model = model
        self._catalog = catalog

    def _load_effects_json(self):
        return self._catalog if self._catalog is not None else super()._load_effects_json()


def _scene(name, param="AQ=="):
    return {"sceneName": name, "lightEffects": [{"scenceParam": param}]}


def test_effect_map_disambiguates_duplicate_names():
    catalog = {"data": {"categories": [
        {"categoryName": "Festival", "scenes": [_scene("Halloween"), _scene("Halloween"), _scene("Party")]},
        {"categoryName": "Nature", "scenes": [_scene("Halloween")]},
    ]}}

    names = list(_Catalog("H6095", catalog)._effect_map())

    assert names == ["Halloween (Festival)", "Halloween (Festival) #2", "Party", "Halloween (Nature)"]


@pytest.mark.parametrize("model", ["H6095", "H3501", "H7093"])
def test_shipped_catalog_resolves_every_effect(model):
    catalog = _Catalog(model)
    effect_map = catalog._effect_map()

    assert effect_map
    for indexes in effect_map.values():
        assert catalog._resolve_effect_param(*indexes)
