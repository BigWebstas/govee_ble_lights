import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakCharacteristicNotFoundError
from homeassistant.components.light import ATTR_EFFECT, ATTR_RGB_COLOR

from custom_components.govee_ble_lights import light
from custom_components.govee_ble_lights.light import GoveeAPILight, GoveeBLEControlMixin, GoveeHybridLight

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
    entity.async_write_ha_state = MagicMock()
    return entity


def _api_light():
    hub = MagicMock()
    hub.api = AsyncMock()
    entity = GoveeAPILight(hub, DEVICE)
    entity.async_write_ha_state = MagicMock()
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


@pytest.mark.parametrize("make_entity", [lambda: _hybrid(AsyncMock()), _api_light])
async def test_active_effect_shown_then_cleared_by_color(make_entity):
    entity = make_entity()
    scene = {"name": "Aurora", "value": {"id": 1, "paramId": 2}}

    with patch.object(light, "_resolve_cloud_scene", AsyncMock(return_value=scene)):
        await entity.async_turn_on(**{ATTR_EFFECT: "Aurora"})
    assert entity.effect == "Aurora"
    entity.async_write_ha_state.assert_called()

    await entity.async_turn_on(**{ATTR_RGB_COLOR: (255, 0, 0)})
    assert entity.effect is None


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


def test_ble_effect_list_is_sorted_alphabetically():
    catalog = {"data": {"categories": [
        {"categoryName": "A", "scenes": [_scene("zebra"), _scene("Apple"), _scene("banana")]},
    ]}}

    assert _Catalog("H6095", catalog)._local_ble_effect_list() == ["Apple", "banana", "zebra"]


@pytest.mark.parametrize("model", ["H6095", "H3501", "H7093", "H70B1"])
def test_shipped_catalog_resolves_every_effect(model):
    catalog = _Catalog(model)
    effect_map = catalog._effect_map()

    assert effect_map
    for indexes in effect_map.values():
        assert catalog._resolve_effect_param(*indexes)


def test_h70b1_scene_matches_govee_app_capture():
    # Greedy Snake as captured from the Govee Home app over BLE.
    light_ = object.__new__(GoveeBLEControlMixin)
    light_._model = "H70B1"
    catalog = _Catalog("H70B1")
    name = "Greedy Snake"
    scene = base64.b64decode(catalog._resolve_effect_param(*catalog._effect_map()[name]))

    packets = [bytes(p).hex() for p in light_._build_scene_packets(scene)]

    assert packets == [
        "a30001030c541d000303320f08ff0000ff7f00ae",
        "a301ffff0000ff000000ff00ffff8b00ffffffd6",
        "a3ffff00000000000000000000000000000000a3",
    ]


def _make_ble_light():
    hub = MagicMock(address="AA:BB:CC:DD:EE:FF")
    entry = MagicMock(data={"model": "H705A"})
    entity = light.GoveeBluetoothLight(hub, "cached-device", entry)
    entity.hass = MagicMock()
    entity.async_write_ha_state = MagicMock()
    return entity


@pytest.fixture
def ble(monkeypatch):
    """Patch the Bluetooth stack; return (client, establish_connection mock, device lookup mock)."""
    client = MagicMock(write_gatt_char=AsyncMock(), disconnect=AsyncMock(),
                       read_gatt_char=AsyncMock(side_effect=BleakCharacteristicNotFoundError("2b12")),
                       start_notify=AsyncMock())
    establish = AsyncMock(return_value=client)
    lookup = MagicMock(return_value="fresh-device")
    monkeypatch.setattr(light.bleak_retry_connector, "establish_connection", establish)
    monkeypatch.setattr(light.bluetooth, "async_ble_device_from_address", lookup)
    monkeypatch.setattr(light, "BLE_DISCONNECT_DELAY", 0)
    monkeypatch.setattr(light, "BLE_PROBE_TIMEOUT", 0.01)
    return client, establish, lookup


async def test_ble_write_uses_fresh_device_and_disconnects(ble):
    client, establish, lookup = ble
    entity = _make_ble_light()

    await entity.async_turn_off()

    lookup.assert_called_once_with(entity.hass, "AA:BB:CC:DD:EE:FF", True)
    assert establish.call_args.args[1] == "fresh-device"
    assert client.write_gatt_char.await_count == 2  # v1 probe (unanswered), then the plain command
    await entity._ble_close_task
    client.disconnect.assert_awaited_once()
    assert entity.is_on is False


async def test_ble_call_returns_before_disconnect_and_lock_is_held_until_closed(ble):
    client, *_ = ble
    release = asyncio.Event()
    client.disconnect.side_effect = release.wait
    entity = _make_ble_light()

    await entity.async_turn_off()

    assert entity.is_on is False and entity._ble_lock.locked()  # returned while the link is still closing
    release.set()
    await entity._ble_close_task
    client.disconnect.assert_awaited_once()
    assert not entity._ble_lock.locked()


async def test_next_command_waits_for_the_previous_link_to_close(ble):
    client, establish, _ = ble
    release = asyncio.Event()
    client.disconnect.side_effect = release.wait
    entity = _make_ble_light()
    await entity.async_turn_off()

    second = asyncio.create_task(entity.async_turn_on())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert establish.await_count == 1  # still blocked behind the closing link
    release.set()
    await second
    assert establish.await_count == 2


async def test_learned_scheme_is_saved_and_restored(ble, monkeypatch):
    store = MagicMock(async_load=AsyncMock(return_value={"scheme": "v1"}), async_delay_save=MagicMock())
    monkeypatch.setattr(light, "Store", MagicMock(return_value=store))
    entity = _make_ble_light()

    await entity.async_added_to_hass()
    assert entity._ble_scheme == "v1"

    entity._set_ble_scheme("plain")
    assert store.async_delay_save.call_args.args[0]() == {"scheme": "plain"}


async def test_garbage_in_the_store_is_ignored(ble, monkeypatch):
    store = MagicMock(async_load=AsyncMock(return_value={"scheme": "rot13"}))
    monkeypatch.setattr(light, "Store", MagicMock(return_value=store))
    entity = _make_ble_light()

    await entity.async_added_to_hass()

    assert entity._ble_scheme is None


async def test_ble_disconnects_even_when_write_fails(ble):
    client, *_ = ble
    client.write_gatt_char.side_effect = OSError("boom")
    entity = _make_ble_light()

    with pytest.raises(OSError):
        await entity.async_turn_off()

    client.disconnect.assert_awaited_once()


async def test_ble_state_unchanged_when_connect_fails(ble):
    _, establish, _ = ble
    establish.side_effect = OSError("no link")
    entity = _make_ble_light()

    with pytest.raises(ConnectionError):
        await entity.async_turn_on()

    assert entity.is_on is None


async def test_ble_falls_back_to_cached_device(ble):
    _, establish, lookup = ble
    lookup.return_value = None
    entity = _make_ble_light()

    await entity.async_turn_on()

    assert establish.call_args.args[1] == "cached-device"
    assert entity.is_on is True


async def test_encrypted_light_gets_handshake_then_sealed_frames(ble):
    from custom_components.govee_ble_lights import govee_crypto as gc

    client, *_ = ble
    client.read_gatt_char = AsyncMock(return_value=bytes.fromhex("0102") + bytes(18))
    captured = {}

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    writes = []

    async def write(_uuid, data, _response):
        writes.append(bytes(data))
        if len(writes) == 1:  # the handshake request: answer as a light would
            iv = bytes(range(12))
            header = bytes([0xE7, 0x11, 0x00]) + iv
            payload = bytes(8) + b"H3501" + bytes(6)
            captured["notify"](None, header + gc._gcm_encrypt(gc.KEY_HANDSHAKE, iv, payload, header))

    client.start_notify = start_notify
    client.write_gatt_char = write
    entity = _make_ble_light()

    await entity.async_turn_off()

    plain = entity._build_ble_off_command()
    assert writes[0][:3] == bytes([0xE7, 0x11, 0x01])
    assert len(writes) == 2 and writes[1] != plain and len(writes[1]) == 4 + 20 + gc.TAG_LEN
    assert entity.is_on is False


async def test_encrypted_handshake_timeout_raises_and_keeps_state(ble, monkeypatch):
    client, *_ = ble
    client.read_gatt_char = AsyncMock(return_value=bytes.fromhex("0102"))
    client.start_notify = AsyncMock()
    monkeypatch.setattr(light, "BLE_HANDSHAKE_TIMEOUT", 0.01)
    entity = _make_ble_light()

    with pytest.raises(ConnectionError, match="handshake"):
        await entity.async_turn_on()

    assert entity.is_on is None
    client.disconnect.assert_awaited_once()
    assert entity._ble_scheme is None  # a remembered scheme that stops answering is forgotten


def _legacy_light_reply(request: bytes, key: bytes) -> bytes:
    from custom_components.govee_ble_lights import govee_crypto as gc

    body = bytes([0xE7, 0x01]) + key + b"\0"
    return gc._ecb_rc4(gc.KEY_COMM, body + bytes([gc._xor_checksum(body)]), True)


async def test_legacy_encrypted_light_is_detected_and_sealed(ble):
    from custom_components.govee_ble_lights import govee_crypto as gc

    client, *_ = ble
    key = bytes(range(16))
    captured = {}
    writes = []

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    async def write(_uuid, data, _response):
        writes.append(bytes(data))
        if len(writes) % 3 == 1:  # round 1 of each connection: the light answers with its session key
            captured["notify"](None, _legacy_light_reply(bytes(data), key))

    client.start_notify = start_notify
    client.write_gatt_char = write
    entity = _make_ble_light()

    await entity.async_turn_off()

    plain = entity._build_ble_off_command()
    assert len(writes) == 3  # request, confirm, command
    assert writes[2] == gc._ecb_rc4(key, plain, True) != plain
    assert entity._ble_scheme == "v1"

    await entity.async_turn_off()  # known scheme: a fresh handshake on the next connection
    assert len(writes) == 6 and entity._ble_scheme == "v1"


async def test_silent_light_gets_plain_frames_and_is_not_pinned(ble):
    client, *_ = ble
    entity = _make_ble_light()

    await entity.async_turn_off()

    plain = entity._build_ble_off_command()
    assert client.write_gatt_char.await_args_list[-1].args[1] == plain
    assert entity._ble_scheme is None


@pytest.mark.parametrize("model,ha_value,byte", [("H70B1", 38, 15), ("H3501", 255, 100), ("H619D", 128, 50),
                                                 ("H705A", 51, 20), ("H705A", 255, 100), ("H6159", 38, 15), ("H6159", 0, 0)])
def test_ble_brightness_byte_matches_the_lights_scale(model, ha_value, byte):
    light_ = object.__new__(GoveeBLEControlMixin)
    light_._model = model
    light_._is_segmented = False

    commands = light_._build_ble_on_commands(brightness=ha_value)

    assert commands[1][1] == 0x04 and commands[1][2] == byte


async def test_silent_light_is_cached_as_plain_after_three_misses(ble):
    client, *_ = ble
    entity = _make_ble_light()

    for _ in range(light.BLE_PLAIN_AFTER_MISSES):
        assert entity._ble_scheme is None
        await entity.async_turn_off()
    assert entity._ble_scheme == "plain"

    client.read_gatt_char.reset_mock()
    client.start_notify.reset_mock()
    client.write_gatt_char.reset_mock()
    await entity.async_turn_off()

    client.read_gatt_char.assert_not_awaited()
    client.start_notify.assert_not_awaited()
    client.write_gatt_char.assert_awaited_once()
    assert client.write_gatt_char.await_args.args[1] == entity._build_ble_off_command()


async def test_positive_detection_resets_the_miss_counter(ble):
    client, *_ = ble
    entity = _make_ble_light()
    entity._ble_probe_misses = 2
    captured = {}

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    async def write(_uuid, data, _response):
        if len(client.sent) % 3 == 0:
            captured["notify"](None, _legacy_light_reply(bytes(data), bytes(range(16))))
        client.sent.append(bytes(data))

    client.sent = []
    client.start_notify = start_notify
    client.write_gatt_char = write

    await entity.async_turn_off()

    assert entity._ble_scheme == "v1" and entity._ble_probe_misses == 0


async def test_flaky_marker_read_is_not_a_miss(ble):
    client, *_ = ble
    client.read_gatt_char = AsyncMock(side_effect=TimeoutError("flaky"))
    entity = _make_ble_light()

    with pytest.raises(TimeoutError):
        await entity.async_turn_off()

    assert entity._ble_probe_misses == 0 and entity._ble_scheme is None
    client.write_gatt_char.assert_not_awaited()


async def test_plain_cache_is_dropped_when_the_connection_fails(ble):
    _, establish, _ = ble
    establish.side_effect = OSError("gone")
    entity = _make_ble_light()
    entity._ble_scheme, entity._ble_probe_misses = "plain", 3

    with pytest.raises(ConnectionError):
        await entity.async_turn_off()

    assert entity._ble_scheme is None and entity._ble_probe_misses == 0
