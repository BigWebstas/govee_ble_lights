import pytest

from custom_components.govee_ble_lights.govee_api import GoveeAPI

OPTIONS = [{"name": "Aurora", "value": {"id": 1, "paramId": 2}}]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"payload": {"capabilities": [{"parameters": {"options": OPTIONS}}]}}, OPTIONS),
        ({"payload": {"capabilities": [{"parameters": {"options": []}}]}}, []),
        ({"payload": {"capabilities": [{"parameters": {}}]}}, []),
        ({"payload": {"capabilities": [{}]}}, []),
        ({"payload": {"capabilities": []}}, []),
        ({"payload": {}}, []),
        ({}, []),
    ],
)
def test_scene_options_tolerates_missing_levels(response, expected):
    assert GoveeAPI._scene_options(response) == expected
