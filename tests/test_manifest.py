import json
from pathlib import Path

from homeassistant.core import valid_domain

from custom_components.govee_ble_lights.const import DOMAIN

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "govee_ble_lights"


def test_domain_is_valid_and_consistent():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())

    # HA refuses to serve brand icons for an invalid domain (e.g. with hyphens).
    assert valid_domain(DOMAIN)
    assert manifest["domain"] == DOMAIN == INTEGRATION.name


def test_brand_icon_is_shipped():
    assert (INTEGRATION / "brand" / "icon.png").is_file()
