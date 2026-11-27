import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakCharacteristicNotFoundError, BleakError
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


_BLE_ENTITIES: list[light.GoveeBluetoothLight] = []


def _make_ble_light():
    hub = MagicMock(address="AA:BB:CC:DD:EE:FF")
    entry = MagicMock(data={"model": "H705A"})
    entity = light.GoveeBluetoothLight(hub, "cached-device", entry)
    entity.hass = MagicMock()
    entity.async_write_ha_state = MagicMock()
    _BLE_ENTITIES.append(entity)
    return entity


@pytest.fixture
def ble(monkeypatch):
    """Patch the Bluetooth stack; return (client, establish_connection mock, device lookup mock)."""
    _BLE_ENTITIES.clear()
    client = MagicMock(is_connected=True, write_gatt_char=AsyncMock(), disconnect=AsyncMock(),
                       read_gatt_char=AsyncMock(side_effect=BleakCharacteristicNotFoundError("2b12")),
                       start_notify=AsyncMock())
    establish = AsyncMock(return_value=client)
    lookup = MagicMock(return_value="fresh-device")
    monkeypatch.setattr(light.bleak_retry_connector, "establish_connection", establish)
    monkeypatch.setattr(light.bluetooth, "async_ble_device_from_address", lookup)
    monkeypatch.setattr(light, "BLE_IDLE_HOLD", 60)  # tests that want the idle close shorten it
    monkeypatch.setattr(light, "BLE_SEGMENT_BATCH_WINDOW", 0.01)
    monkeypatch.setattr(light, "BLE_PROBE_TIMEOUT", 0.01)
    monkeypatch.setattr(light, "BLE_VERIFY_DELAY", 0)
    monkeypatch.setattr(light, "BLE_VERIFY_TIMEOUT", 0.01)
    monkeypatch.setattr(light, "BLE_V1_CONFIRM_DELAY", 0)
    monkeypatch.setattr(light.bluetooth, "async_register_callback", MagicMock())
    monkeypatch.setattr(light.bluetooth, "async_track_unavailable", MagicMock())
    monkeypatch.setattr(light, "async_call_later", MagicMock())
    monkeypatch.setattr(light, "async_track_time_interval", MagicMock())
    monkeypatch.setattr(light.GoveeBluetoothLight, "async_get_last_state", AsyncMock(return_value=None))
    yield client, establish, lookup
    for entity in _BLE_ENTITIES:
        entity._cancel_on_retry()
        entity._cancel_off_retry()
        entity._cancel_idle_close()
    _BLE_ENTITIES.clear()


async def test_ble_write_uses_fresh_device_and_keeps_link_open(ble):
    client, establish, lookup = ble
    entity = _make_ble_light()

    await entity.async_turn_off()

    lookup.assert_called_once_with(entity.hass, "AA:BB:CC:DD:EE:FF", True)
    assert establish.call_args.args[0] is light.BleakClientWithServiceCache
    assert establish.call_args.args[1] == "fresh-device"
    assert establish.call_args.kwargs["disconnected_callback"] == entity._on_ble_disconnected
    assert client.write_gatt_char.await_count == 3  # v1 probe (unanswered), plain off, aa 01 query
    client.disconnect.assert_not_awaited()  # held open for the next command
    assert entity.is_on is False
    await entity._close_ble()
    client.disconnect.assert_awaited_once()


async def test_burst_of_commands_shares_one_connection(ble):
    client, establish, _ = ble
    entity = _make_ble_light()

    await entity.async_turn_off()
    await entity.async_turn_on()

    assert establish.await_count == 1
    assert client.write_gatt_char.await_count == 5  # probe, off + aa01, on + aa01
    await entity._close_ble()


async def test_link_closes_after_idle_hold(ble, monkeypatch):
    client, *_ = ble
    monkeypatch.setattr(light, "BLE_IDLE_HOLD", 0.01)
    entity = _make_ble_light()

    await entity.async_turn_off()
    await asyncio.sleep(0.05)

    client.disconnect.assert_awaited_once()
    assert entity._ble_client is None


async def test_new_command_pushes_back_the_idle_close(ble, monkeypatch):
    client, establish, _ = ble
    monkeypatch.setattr(light, "BLE_IDLE_HOLD", 0.05)
    entity = _make_ble_light()

    await entity.async_turn_off()
    await asyncio.sleep(0.03)
    await entity.async_turn_on()
    await asyncio.sleep(0.03)

    client.disconnect.assert_not_awaited()  # first timer was cancelled by the second write
    assert establish.await_count == 1
    await asyncio.sleep(0.05)
    client.disconnect.assert_awaited_once()


async def test_dropped_link_is_replaced_with_a_fresh_connection(ble):
    client, establish, _ = ble
    entity = _make_ble_light()
    await entity.async_turn_off()

    client.is_connected = False  # the light hung up while idle
    await entity.async_turn_on()

    assert establish.await_count == 2
    await entity._close_ble()


async def test_failed_write_on_kept_link_retries_once_on_fresh_link(ble):
    client, establish, _ = ble
    entity = _make_ble_light()
    entity._ble_scheme = "plain"
    await entity.async_turn_off()  # link now kept

    client.write_gatt_char.side_effect = [OSError("stale link"), None, None]
    await entity.async_turn_on()

    assert establish.await_count == 2
    assert entity.is_on is True
    await entity._close_ble()


async def test_failed_write_on_fresh_link_is_not_retried(ble):
    client, establish, _ = ble
    client.write_gatt_char.side_effect = OSError("boom")
    entity = _make_ble_light()

    await entity.async_turn_off()

    assert establish.await_count == 1
    client.disconnect.assert_awaited_once()
    assert entity._ble_client is None
    assert entity._off_retry_handle is not None


async def test_connect_is_attempted_once(ble):
    _, establish, _ = ble
    establish.side_effect = OSError("no link")
    entity = _make_ble_light()

    with pytest.raises(ConnectionError):
        await entity.async_turn_on()

    assert establish.await_count == 1


async def test_removal_closes_an_open_link(ble):
    client, *_ = ble
    entity = _make_ble_light()
    await entity.async_turn_off()

    await entity.async_will_remove_from_hass()

    client.disconnect.assert_awaited_once()


async def test_learned_scheme_is_saved_and_restored(ble, monkeypatch):
    store = MagicMock(async_load=AsyncMock(return_value={"scheme": "v1"}), async_delay_save=MagicMock())
    monkeypatch.setattr(light, "Store", MagicMock(return_value=store))
    entity = _make_ble_light()

    await entity.async_added_to_hass()
    assert entity._ble_scheme == "v1"

    entity._set_ble_scheme("plain")
    assert store.async_delay_save.call_args.args[0]()["scheme"] == "plain"


async def test_garbage_in_the_store_is_ignored(ble, monkeypatch):
    store = MagicMock(async_load=AsyncMock(return_value={"scheme": "rot13"}))
    monkeypatch.setattr(light, "Store", MagicMock(return_value=store))
    entity = _make_ble_light()

    await entity.async_added_to_hass()

    assert entity._ble_scheme is None


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


async def test_encrypted_light_gets_handshake_then_sealed_frames(ble, monkeypatch):
    from custom_components.govee_ble_lights import govee_crypto as gc

    client, *_ = ble
    client.read_gatt_char = AsyncMock(return_value=bytes.fromhex("0102") + bytes(18))
    captured = {}

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    writes = []
    monkeypatch.setattr(light.GoveeBluetoothLight, "_read_power", AsyncMock(return_value=False))

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
    assert entity._ble_scheme == "v2"  # one missed reply does not forget the scheme


def _sealed_off_status(key: bytes) -> bytes:
    from custom_components.govee_ble_lights import govee_crypto as gc

    return gc._ecb_rc4(key, _status_frame(False), True)


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
        if len(writes) % 4 == 1:  # round 1 of each connection: the light answers with its session key
            captured["notify"](None, _legacy_light_reply(bytes(data), key))
        elif len(writes) % 4 == 0:  # the aa 01 query
            captured["notify"](None, gc._ecb_rc4(key, _status_frame(False), True))

    client.start_notify = start_notify
    client.write_gatt_char = write
    entity = _make_ble_light()

    await entity.async_turn_off()

    plain = entity._build_ble_off_command()
    assert len(writes) == 4  # request, confirm, command, aa 01 query
    assert writes[2] == gc._ecb_rc4(key, plain, True) != plain
    assert entity._ble_scheme == "v1"

    await entity._close_ble()
    await entity.async_turn_off()  # known scheme: a fresh handshake on the next connection
    assert len(writes) == 8 and entity._ble_scheme == "v1"


async def test_silent_light_gets_plain_frames_and_is_not_pinned(ble):
    client, *_ = ble
    entity = _make_ble_light()

    await entity.async_turn_off()

    plain = entity._build_ble_off_command()
    assert client.write_gatt_char.await_args_list[-2].args[1] == plain
    assert client.write_gatt_char.await_args_list[-1].args[1] == entity._prepareQueryPacketData(light.LedCommand.POWER)
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
        await entity._close_ble()  # each probe needs its own connection
    assert entity._ble_scheme == "plain"

    client.read_gatt_char.reset_mock()
    client.start_notify.reset_mock()
    client.write_gatt_char.reset_mock()
    await entity.async_turn_off()

    client.read_gatt_char.assert_not_awaited()
    frames = [call.args[1] for call in client.write_gatt_char.await_args_list]
    assert frames == [entity._build_ble_off_command(), entity._prepareQueryPacketData(light.LedCommand.POWER)]


async def test_positive_detection_resets_the_miss_counter(ble):
    client, *_ = ble
    entity = _make_ble_light()
    entity._ble_probe_misses = 2
    captured = {}

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    async def write(_uuid, data, _response):
        key = bytes(range(16))
        if not client.sent:
            captured["notify"](None, _legacy_light_reply(bytes(data), key))
        elif len(client.sent) == 3:
            captured["notify"](None, _sealed_off_status(key))
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

    with pytest.raises(ConnectionError):
        await entity.async_turn_on()

    assert entity._ble_probe_misses == 0 and entity._ble_scheme is None
    client.write_gatt_char.assert_not_awaited()


async def test_plain_cache_is_dropped_when_the_connection_fails(ble):
    _, establish, _ = ble
    establish.side_effect = OSError("gone")
    entity = _make_ble_light()
    entity._ble_scheme, entity._ble_probe_misses = "plain", 3

    with pytest.raises(ConnectionError):
        await entity.async_turn_on()

    assert entity._ble_scheme is None and entity._ble_probe_misses == 0


def test_scene_variants_are_named_after_their_scene():
    entity = _make_ble_light()
    entity._model = "H7093"

    effects = entity.effect_list

    assert {"Halloween A", "Halloween B", "Halloween C", "Christmas A", "Christmas B"} <= set(effects)
    assert not {"A", "B", "C"} & set(effects)


def test_scene_catalog_is_parsed_once_and_effect_list_is_memoised():
    light._read_catalog.cache_clear()
    entity = _make_ble_light()

    first = entity.effect_list
    for _ in range(3):
        assert entity.effect_list == first

    assert light._read_catalog.cache_info().misses == 1


def _frame(*payload):
    frame = bytes([0x33, 0x05, *payload]).ljust(19, b"\0")
    checksum = 0
    for b in frame:
        checksum ^= b
    return frame + bytes([checksum])


def test_segment_color_frame_selects_one_segment():
    entity = _make_ble_light()
    assert entity._build_segment_color_command(1 << 0, 255, 0, 0) == _frame(0x15, 0x01, 255, 0, 0, 0, 0, 0, 0, 0, 0x01, 0x00)
    assert entity._build_segment_color_command(1 << 9, 0, 0, 255) == _frame(0x15, 0x01, 0, 0, 255, 0, 0, 0, 0, 0, 0x00, 0x02)


def test_segment_brightness_frame():
    entity = _make_ble_light()
    assert entity._build_segment_brightness_command(1 << 2, 40) == _frame(0x15, 0x02, 40, 0x04, 0x00)


async def test_ble_segment_writes_through_parent(ble):
    parent = _make_ble_light()
    segment = light.GoveeBLESegmentLight(parent, 1)
    segment.async_write_ha_state = MagicMock()
    parent._write_ble_commands = AsyncMock()

    await segment.async_turn_on(**{ATTR_RGB_COLOR: (1, 2, 3)})
    parent._write_ble_commands.assert_awaited_once_with([parent._build_segment_color_command(1 << 1, 1, 2, 3)])
    assert segment.is_on and segment.rgb_color == (1, 2, 3)

    parent._write_ble_commands.reset_mock()
    await segment.async_turn_off()
    parent._write_ble_commands.assert_awaited_once_with([parent._build_segment_brightness_command(1 << 1, 0)])
    assert segment.is_on is False
    assert segment.unique_id == f"{parent.unique_id}_segment_1"
    assert segment.device_info == parent.device_info


async def test_setup_adds_segments_only_for_segmented_ble_models(monkeypatch):
    for model, expected in (("H70B1", 1 + 15), ("H705A", 1)):
        hub = MagicMock(address="AA:BB:CC:DD:EE:FF", devices=None)
        entry = MagicMock(entry_id="e", data={"model": model})
        hass = MagicMock(data={light.DOMAIN: {"e": hub}}, async_add_executor_job=AsyncMock())
        monkeypatch.setattr(light.bluetooth, "async_ble_device_from_address", MagicMock())
        add = MagicMock()
        await light.async_setup_entry(hass, entry, add)
        assert len(add.call_args.args[0]) == expected


async def test_api_segment_still_calls_cloud_api():
    hub = MagicMock(api=MagicMock(set_segment_rgb=AsyncMock(), set_segment_brightness=AsyncMock()))
    segment = light.GoveeAPISegmentLight(hub, {**DEVICE, "deviceName": "Strip"}, 2)
    segment.async_write_ha_state = MagicMock()

    await segment.async_turn_on(**{ATTR_RGB_COLOR: (9, 8, 7), "brightness": 255})
    hub.api.set_segment_rgb.assert_awaited_once_with(DEVICE["sku"], DEVICE["device"], [2], 9, 8, 7)
    hub.api.set_segment_brightness.assert_awaited_once_with(DEVICE["sku"], DEVICE["device"], [2], 100)

    await segment.async_turn_off()
    hub.api.set_segment_brightness.assert_awaited_with(DEVICE["sku"], DEVICE["device"], [2], 0)


def _segments(parent, count=4):
    segments = [light.GoveeBLESegmentLight(parent, index) for index in range(count)]
    for segment in segments:
        segment.async_write_ha_state = MagicMock()
    return segments


async def test_segments_set_together_share_a_frame_and_a_connection(ble):
    client, establish, _ = ble
    parent = _make_ble_light()
    parent._ble_scheme = "plain"  # no probe writes to count
    segments = _segments(parent)

    await asyncio.gather(*(s.async_turn_on(**{ATTR_RGB_COLOR: (255, 0, 0)}) for s in segments[:3]))

    assert establish.await_count == 1
    frames = [call.args[1] for call in client.write_gatt_char.await_args_list]
    assert frames == [parent._build_segment_color_command(0b111, 255, 0, 0)]
    assert all(s.is_on for s in segments[:3])
    await parent._close_ble()


async def test_segments_with_different_colours_get_one_frame_each(ble):
    client, establish, _ = ble
    parent = _make_ble_light()
    parent._ble_scheme = "plain"
    segments = _segments(parent)

    await asyncio.gather(
        segments[0].async_turn_on(**{ATTR_RGB_COLOR: (255, 0, 0)}),
        segments[1].async_turn_on(**{ATTR_RGB_COLOR: (0, 0, 255)}),
        segments[2].async_turn_on(**{ATTR_RGB_COLOR: (255, 0, 0)}),
    )

    assert establish.await_count == 1
    frames = [call.args[1] for call in client.write_gatt_char.await_args_list]
    assert frames == [parent._build_segment_color_command(0b101, 255, 0, 0),
                      parent._build_segment_color_command(0b010, 0, 0, 255)]
    await parent._close_ble()


async def test_segment_queued_twice_keeps_its_last_colour(ble):
    client, *_ = ble
    parent = _make_ble_light()
    parent._ble_scheme = "plain"
    segment = _segments(parent, 1)[0]

    await asyncio.gather(segment._send_color(255, 0, 0), segment._send_color(0, 255, 0))

    frames = [call.args[1] for call in client.write_gatt_char.await_args_list]
    assert frames == [parent._build_segment_color_command(0b1, 0, 255, 0)]
    await parent._close_ble()


async def test_failed_segment_batch_raises_in_every_caller_and_keeps_state(ble):
    _, establish, _ = ble
    establish.side_effect = OSError("no link")
    parent = _make_ble_light()
    segments = _segments(parent, 2)

    results = await asyncio.gather(
        *(s.async_turn_on(**{ATTR_RGB_COLOR: (1, 2, 3)}) for s in segments), return_exceptions=True)

    assert all(isinstance(r, ConnectionError) for r in results)
    assert all(s.is_on is None for s in segments)


async def test_turn_off_that_cannot_connect_is_retried_later(ble, monkeypatch):
    client, establish, _ = ble
    monkeypatch.setattr(light, "BLE_RETRY_DELAYS", (0.01, 0.01, 0.01))
    establish.side_effect = [OSError("no link"), client]
    entity = _make_ble_light()

    await entity.async_turn_off()  # swallowed: no error reaches the caller
    assert entity.is_on is None

    await asyncio.sleep(0.1)
    assert entity.is_on is False
    assert establish.await_count == 2
    await entity._close_ble()


async def test_failed_off_retry_gives_up_quietly(ble, monkeypatch):
    _, establish, _ = ble
    monkeypatch.setattr(light, "BLE_RETRY_DELAYS", (0.01, 0.01, 0.01))
    establish.side_effect = OSError("no link")
    entity = _make_ble_light()

    await entity.async_turn_off()
    await asyncio.sleep(0.2)

    assert establish.await_count == 4  # the command and three retries
    assert entity.is_on is None
    assert entity.available is False


async def test_turn_on_cancels_a_pending_off_retry(ble, monkeypatch):
    client, establish, _ = ble
    monkeypatch.setattr(light, "BLE_RETRY_DELAYS", (0.05,))
    establish.side_effect = [OSError("no link"), client]
    entity = _make_ble_light()

    await entity.async_turn_off()
    await entity.async_turn_on()
    await asyncio.sleep(0.15)

    assert entity.is_on is True
    assert establish.await_count == 2
    await entity._close_ble()


def test_h6095_scene_matches_govee_app_capture():
    # Aurora as captured from the Govee Home app over BLE (decrypted v1 frames, 2026-11-17).
    light_ = object.__new__(GoveeBLEControlMixin)
    light_._model = "H6095"
    catalog = _Catalog("H6095")
    scene = base64.b64decode(catalog._resolve_effect_param(*catalog._effect_map()["Aurora"]))

    packets = [bytes(p).hex() for p in light_._build_scene_packets(scene)]

    assert packets == [
        "a30001035822000001001d0001000000137f00aa",
        "a30103006400030000ff00ffff6d62ff32000af1",
        "a3ff0a641e1e1e0000000000000000000000002c",
    ]
    # The app then activates it with the catalog sceneCode (19516 = 0x4c3c); the app's extra byte (02 for Aurora) is not understood, so we send 00.
    assert light_._build_scene_activate_command(19516)[:6].hex() == "3305043c4c00"


def _status_frame(is_on: bool) -> bytes:
    body = bytes([0xAA, 0x01, 0x01 if is_on else 0x00]).ljust(19, b"\0")
    checksum = 0
    for b in body:
        checksum ^= b
    return body + bytes([checksum])


async def test_turn_on_retries_when_light_reports_off_then_succeeds(ble):
    client, establish, _ = ble
    entity = _make_ble_light()
    entity._ble_scheme = "plain"
    replies = iter([_status_frame(False), _status_frame(True)])
    captured = {}
    writes = []

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    async def write(_uuid, data, _response):
        frame = bytes(data)
        writes.append(frame)
        if frame[:2] == bytes([0xAA, 0x01]):
            captured["notify"](None, next(replies))

    client.start_notify = start_notify
    client.write_gatt_char = write

    await entity.async_turn_on()

    power_on = entity._prepareSinglePacketData(light.LedCommand.POWER, [0x01])
    assert writes.count(power_on) == 2
    assert establish.await_count == 1
    assert entity.is_on is True
    assert entity._ble_supports_status is True
    await entity._close_ble()


async def test_turn_on_raises_and_keeps_state_when_light_stays_off(ble):
    client, *_ = ble
    entity = _make_ble_light()
    entity._ble_scheme = "plain"
    captured = {}

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    async def write(_uuid, data, _response):
        if bytes(data)[:2] == bytes([0xAA, 0x01]):
            captured["notify"](None, _status_frame(False))

    client.start_notify = start_notify
    client.write_gatt_char = write

    with pytest.raises(ConnectionError, match="did not turn on"):
        await entity.async_turn_on()

    assert entity.is_on is None
    entity._cancel_on_retry()
    await entity._close_ble()


async def test_turn_off_retries_when_light_reports_on_then_succeeds(ble):
    client, *_ = ble
    entity = _make_ble_light()
    entity._ble_scheme = "plain"
    replies = iter([_status_frame(True), _status_frame(False)])
    captured = {}
    writes = []

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    async def write(_uuid, data, _response):
        frame = bytes(data)
        writes.append(frame)
        if frame[:2] == bytes([0xAA, 0x01]):
            captured["notify"](None, next(replies))

    client.start_notify = start_notify
    client.write_gatt_char = write

    await entity.async_turn_off()

    power_off = entity._build_ble_off_command()
    assert writes.count(power_off) == 2
    assert entity.is_on is False
    await entity._close_ble()


async def test_turn_on_verifies_encrypted_v1_and_v2_status_replies(ble):
    from custom_components.govee_ble_lights import govee_crypto as gc

    # v1 encrypted verification
    client, *_ = ble
    key = bytes(range(16))
    captured = {}
    writes = []

    async def start_notify_v1(_uuid, callback):
        captured["notify"] = callback

    async def write_v1(_uuid, data, _response):
        raw = bytes(data)
        writes.append(raw)
        if len(writes) == 1:
            captured["notify"](None, _legacy_light_reply(raw, key))
        elif len(writes) >= 3:
            plain = gc._ecb_rc4(key, raw, False)
            if plain[:2] == bytes([0xAA, 0x01]):
                captured["notify"](None, gc._ecb_rc4(key, _status_frame(True), True))

    client.start_notify = start_notify_v1
    client.write_gatt_char = write_v1
    v1_entity = _make_ble_light()

    await v1_entity.async_turn_on()
    assert v1_entity.is_on is True and v1_entity._ble_supports_status is True
    await v1_entity._close_ble()

    # v2 AES-GCM encrypted verification
    device_iv_key = bytes(range(10, 18))
    sku, mac_wire = b"H3501", bytes(range(6))
    device_key = gc.derive_device_key(sku, mac_wire)
    client.read_gatt_char = AsyncMock(return_value=bytes.fromhex("0102") + bytes(18))
    v2_writes = []
    dev_counter = 0

    async def start_notify_v2(_uuid, callback):
        captured["notify"] = callback

    async def write_v2(_uuid, data, _response):
        nonlocal dev_counter
        raw = bytes(data)
        v2_writes.append(raw)
        if len(v2_writes) == 1:
            iv = bytes(range(12))
            header = bytes([0xE7, 0x11, 0x00]) + iv
            payload = device_iv_key + sku + mac_wire
            captured["notify"](None, header + gc._gcm_encrypt(gc.KEY_HANDSHAKE, iv, payload, header))
        elif len(v2_writes) == 3:  # sealed aa 01 query
            dev_counter += 1
            ctr = dev_counter.to_bytes(4, "big")
            reply = ctr + gc._gcm_encrypt(device_key, device_iv_key + ctr, _status_frame(True), ctr)
            captured["notify"](None, reply)

    client.start_notify = start_notify_v2
    client.write_gatt_char = write_v2
    v2_entity = _make_ble_light()

    await v2_entity.async_turn_on()
    assert v2_entity.is_on is True and v2_entity._ble_supports_status is True
    await v2_entity._close_ble()


async def test_silently_dead_kept_link_is_detected_by_missing_status_reply(ble):
    client, establish, _ = ble
    entity = _make_ble_light()
    entity._ble_scheme = "plain"
    captured = {}
    silence_next = False

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    async def write(_uuid, data, _response):
        nonlocal silence_next
        if bytes(data)[:2] == bytes([0xAA, 0x01]):
            if silence_next:
                silence_next = False
                return  # simulates a half-dead kept link dropping writes silently
            captured["notify"](None, _status_frame(True))

    client.start_notify = start_notify
    client.write_gatt_char = write

    await entity.async_turn_on()  # learns _ble_supports_status = True
    assert establish.await_count == 1

    silence_next = True
    await entity.async_turn_on()  # reused link goes silent -> reconnects fresh and succeeds
    assert establish.await_count == 2
    assert entity.is_on is True
    await entity._close_ble()


async def test_turn_on_that_cannot_connect_raises_and_retries_once_later(ble, monkeypatch):
    client, establish, _ = ble
    monkeypatch.setattr(light, "BLE_RETRY_DELAYS", (0.01, 0.01, 0.01))
    establish.side_effect = [OSError("no link"), client]
    entity = _make_ble_light()

    with pytest.raises(ConnectionError):
        await entity.async_turn_on(brightness=128)
    assert entity.is_on is None

    await asyncio.sleep(0.1)
    assert entity.is_on is True
    assert entity.brightness == 128
    assert establish.await_count == 2
    await entity._close_ble()


async def test_turn_off_cancels_a_pending_on_retry(ble, monkeypatch):
    client, establish, _ = ble
    monkeypatch.setattr(light, "BLE_RETRY_DELAYS", (0.05,))
    establish.side_effect = [OSError("no link"), client]
    entity = _make_ble_light()

    with pytest.raises(ConnectionError):
        await entity.async_turn_on()

    await entity.async_turn_off()
    await asyncio.sleep(0.15)

    assert entity.is_on is False
    assert establish.await_count == 2
    await entity._close_ble()


async def test_disconnected_callback_clears_cached_link_immediately(ble):
    client, establish, _ = ble
    entity = _make_ble_light()
    entity._ble_scheme = "plain"

    await entity.async_turn_on()
    assert entity._ble_client is client
    assert entity._ble_idle_handle is not None

    callback = establish.call_args.kwargs["disconnected_callback"]
    callback(client)

    assert entity._ble_client is None
    assert entity._ble_idle_handle is None
    assert entity._ble_notify_started is False


async def test_connecting_light_preempts_sibling_idle_link(ble):
    client1 = MagicMock(is_connected=True, write_gatt_char=AsyncMock(), disconnect=AsyncMock(),
                        read_gatt_char=AsyncMock(side_effect=BleakCharacteristicNotFoundError("2b12")),
                        start_notify=AsyncMock())
    client2 = MagicMock(is_connected=True, write_gatt_char=AsyncMock(), disconnect=AsyncMock(),
                        read_gatt_char=AsyncMock(side_effect=BleakCharacteristicNotFoundError("2b12")),
                        start_notify=AsyncMock())
    _, establish, _ = ble
    establish.side_effect = [client1, client2]

    shared_hass = MagicMock(data={})
    e1 = _make_ble_light()
    e1._mac = "AA:BB:CC:DD:EE:01"
    e1._ble_scheme = "plain"
    e1.hass = shared_hass
    e1._ble_light_registry().add(e1)

    e2 = _make_ble_light()
    e2._mac = "AA:BB:CC:DD:EE:02"
    e2._ble_scheme = "plain"
    e2.hass = shared_hass
    e2._ble_light_registry().add(e2)

    await e1.async_turn_on()
    assert e1._ble_client is client1
    client1.disconnect.assert_not_awaited()

    await e2.async_turn_on()
    client1.disconnect.assert_awaited_once()  # preempted before e2 connected
    assert e1._ble_client is None
    assert e2._ble_client is client2
    await e2._close_ble()


async def test_rapid_turn_on_burst_coalesces_to_latest_state(ble):
    client, *_ = ble
    entity = _make_ble_light()
    entity._ble_scheme = "plain"
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    writes = []

    async def write(_uuid, data, _response):
        writes.append(bytes(data))
        if len(writes) == 1:
            first_write_started.set()
            await release_first_write.wait()

    client.write_gatt_char = write

    first = asyncio.create_task(entity.async_turn_on(brightness=25))
    await first_write_started.wait()

    # Fire a burst while the first write holds _ble_lock.
    burst = [
        asyncio.create_task(entity.async_turn_on(brightness=75)),
        asyncio.create_task(entity.async_turn_on(brightness=150, **{ATTR_RGB_COLOR: (10, 20, 30)})),
        asyncio.create_task(entity.async_turn_on(brightness=255)),
    ]
    await asyncio.sleep(0)
    release_first_write.set()
    await asyncio.gather(first, *burst)

    # Only two brightness frames should have gone over BLE: the first (10%) and the merged final (100%).
    brightness_frames = [f for f in writes if f[:2] == bytes([0x33, 0x04])]
    assert [f[2] for f in brightness_frames] == [10, 100]
    color_frames = [f for f in writes if f[:2] == bytes([0x33, 0x05])]
    assert len(color_frames) == 1 and list(color_frames[0][3:6]) == [10, 20, 30]
    assert entity.brightness == 255
    await entity._close_ble()


async def test_h6095_scene_activation_proceeds_immediately_on_a3_ack(ble, monkeypatch):
    client, *_ = ble
    monkeypatch.setattr(light, "BLE_SCENE_ACTIVATE_DELAY", 5.0)  # would time out/crawl if we slept blindly
    entity = _make_ble_light()
    entity._model = "H6095"
    entity._ble_scheme = "plain"
    captured = {}

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    async def write(_uuid, data, _response):
        frame = bytes(data)
        if frame[0] == 0xA3 and frame[1] == 0xFF and "notify" in captured:
            ack = bytes([0xA3, 0x00]).ljust(19, b"\0")
            checksum = 0
            for b in ack:
                checksum ^= b
            captured["notify"](None, ack + bytes([checksum]))

    client.start_notify = start_notify
    client.write_gatt_char = write

    # Pre-open notify (as happens on real v1/v2 or reused links) and trigger an H6095 scene.
    await entity.async_turn_on()
    start = asyncio.get_running_loop().time()
    await entity.async_turn_on(**{ATTR_EFFECT: "Aurora"})
    elapsed = asyncio.get_running_loop().time() - start

    assert elapsed < 1.0  # returned immediately on the 0xA3 ack rather than waiting 5s
    await entity._close_ble()


def test_all_available_ble_models_load_from_compressed_catalog():
    from custom_components.govee_ble_lights.config_flow import _AVAILABLE_MODELS

    assert len(_AVAILABLE_MODELS) == 81
    assert {"H6095", "H3501", "H7093", "H70B1", "H6199", "H6102"} <= set(_AVAILABLE_MODELS)
    for model in _AVAILABLE_MODELS:
        catalog = light._read_catalog(model)
        assert catalog["data"]["categories"]


@pytest.mark.parametrize(("model", "name"), [("H3501", "Rainbow"), ("H7093", "Aurora")])
def test_scene_upload_is_followed_by_activation(model, name):
    light_ = object.__new__(GoveeBLEControlMixin)
    light_._model = model
    light_._is_segmented = False
    catalog = _Catalog(model)
    light_._load_effects_json = catalog._load_effects_json
    indexes = catalog._effect_map()[name]

    commands = light_._build_ble_on_commands(effect=name)

    assert bytes(commands[1])[:5].hex() == "a300010" + str(len(commands) - 3) + "02"
    code = catalog._resolve_effect_code(*indexes[:3])
    assert commands[-1][:6] == bytes([0x33, 0x05, 0x04, *code.to_bytes(2, "little"), 0x00])


async def test_transport_error_on_turn_on_is_retried(ble, monkeypatch):
    client, *_ = ble
    monkeypatch.setattr(light, "BLE_RETRY_DELAYS", (0.01,))
    entity = _make_ble_light()
    entity._ble_scheme = "plain"
    client.write_gatt_char.side_effect = [BleakError("gatt"), None, None, None, None]

    with pytest.raises(ConnectionError, match="gatt"):
        await entity.async_turn_on()
    await asyncio.sleep(0.1)

    assert entity.is_on is True
    await entity._close_ble()


async def test_failure_arriving_after_a_newer_command_schedules_no_retry(ble):
    _, establish, _ = ble
    entity = _make_ble_light()
    release = asyncio.Event()

    async def slow_failure(*_args, **_kwargs):
        await release.wait()
        raise OSError("late")

    establish.side_effect = slow_failure
    on = asyncio.create_task(entity.async_turn_on())
    await asyncio.sleep(0)
    off = asyncio.create_task(entity.async_turn_off())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(on, off, return_exceptions=True)

    assert entity._on_retry_handle is None


async def test_scheme_is_forgotten_after_repeated_handshake_failures_but_never_becomes_plain(ble, monkeypatch):
    client, *_ = ble
    monkeypatch.setattr(light, "BLE_HANDSHAKE_TIMEOUT", 0.01)
    entity = _make_ble_light()
    entity._set_ble_scheme("v1")

    for _ in range(light.BLE_SCHEME_FORGET_AFTER):
        assert entity._ble_scheme == "v1"
        with pytest.raises(ConnectionError, match="handshake"):
            await entity._write_ble_commands([entity._build_ble_off_command()])
    assert entity._ble_scheme is None

    client.write_gatt_char.reset_mock()
    with pytest.raises(ConnectionError, match="handshake"):
        await entity._write_ble_commands([entity._build_ble_off_command()])
    assert client.write_gatt_char.await_count == 1  # only the key request, never a plain command


async def test_silent_encrypted_light_is_not_reported_on(ble):
    entity = _make_ble_light()
    entity._ble_scheme = "v1"
    entity._open_encrypted_session = AsyncMock(return_value=None)

    with pytest.raises(ConnectionError, match="did not confirm"):
        await entity.async_turn_on()

    assert entity.is_on is None
    entity._cancel_on_retry()


async def test_connection_attempts_are_limited(ble):
    client, establish, _ = ble
    hass = MagicMock(data={})
    active = peak = 0

    async def connect(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return client

    establish.side_effect = connect
    entities = [_make_ble_light() for _ in range(5)]
    for entity in entities:
        entity.hass, entity._ble_scheme = hass, "plain"

    await asyncio.gather(*(e.async_turn_off() for e in entities))

    assert peak == light.BLE_CONNECT_LIMIT
    for entity in entities:
        await entity._close_ble()


def _answering(client, power: bool, level: int):
    """Make the fake light answer `aa 01` and `aa 04`."""
    captured = {}

    async def start_notify(_uuid, callback):
        captured["notify"] = callback

    async def write(_uuid, data, _response):
        if data[0] == 0xAA:
            body = bytes([0xAA, data[1], int(power) if data[1] == 0x01 else level]).ljust(19, b"\0")
            checksum = 0
            for byte in body:
                checksum ^= byte
            captured["notify"](None, body + bytes([checksum]))

    client.start_notify = start_notify
    client.write_gatt_char = write


async def test_poll_reads_state_and_releases_the_link(ble):
    client, *_ = ble
    _answering(client, True, 40)
    entity = _make_ble_light()
    entity._ble_scheme = "plain"

    await entity._async_poll()

    assert entity.is_on is True and entity.brightness == 102
    assert "last_confirmed" in entity.extra_state_attributes
    client.disconnect.assert_awaited_once()


async def test_poll_is_skipped_while_a_retry_is_pending(ble):
    _, establish, _ = ble
    entity = _make_ble_light()
    establish.side_effect = OSError("no link")
    await entity.async_turn_off()

    await entity._async_poll()

    assert establish.await_count == 1


async def test_unreachable_light_becomes_unavailable_and_recovers(ble):
    client, establish, _ = ble
    establish.side_effect = OSError("no link")
    entity = _make_ble_light()
    segment = _segments(entity, 1)[0]

    for _ in range(light.BLE_UNAVAILABLE_AFTER):
        await entity._async_poll()
    assert entity.available is False and segment.available is False
    assert "Could not connect" in entity.extra_state_attributes["last_error"]

    establish.side_effect = None
    _answering(client, False, 10)
    await entity._async_poll()
    assert entity.available is True and entity.is_on is False


async def test_light_gone_from_bluetooth_is_unavailable_until_it_advertises(ble):
    entity = _make_ble_light()

    entity._on_gone(None)
    assert entity.available is False

    entity._on_advertisement(None, None)
    assert entity.available is True
    entity.hass.async_create_task.assert_called_once()
    entity.hass.async_create_task.call_args.args[0].close()


async def test_state_is_restored_after_a_restart(ble, monkeypatch):
    monkeypatch.setattr(light.GoveeBluetoothLight, "async_get_last_state",
                        AsyncMock(return_value=MagicMock(state="on", attributes={"brightness": 77})))
    entity = _make_ble_light()

    await entity._restore_state()

    assert entity.is_on is True and entity.brightness == 77

