import pytest

from custom_components.govee_ble_lights import govee_crypto as gc

SKU = b"H3501"
MAC_WIRE = bytes.fromhex("010203040506")
DEVICE_IV_KEY = bytes(range(8))


def _device_reply(request: bytes, status: int = 0) -> bytes:
    """What a light answers to `request`: its ivKey, SKU and MAC sealed under the handshake key."""
    iv = bytes(range(100, 112))
    header = bytes([gc.MAGIC, gc.CMD_SESSION, status]) + iv
    return header + gc._gcm_encrypt(gc.KEY_HANDSHAKE, iv, DEVICE_IV_KEY + SKU + MAC_WIRE, header)


def _client_iv_key(request: bytes) -> bytes:
    return gc._gcm_decrypt(gc.KEY_HANDSHAKE, request[3:15], request[16:], request[:16])


def _open_session() -> tuple[gc.EncryptedSession, bytes]:
    session = gc.EncryptedSession()
    request = session.handshake_request()
    session.accept_handshake_response(_device_reply(request))
    return session, request


def test_handshake_request_layout():
    request = gc.EncryptedSession().handshake_request()

    assert request[:3] == bytes([0xE7, 0x11, 0x01])
    assert request[15] == gc.TAG_LEN
    assert len(request) == 16 + 8 + gc.TAG_LEN
    assert len(_client_iv_key(request)) == 8


def test_sealed_frames_open_with_the_derived_key():
    session, request = _open_session()
    frame = bytes.fromhex("3301010000000000000000000000000000000033")

    sealed = session.seal(frame)

    device_key = gc.derive_device_key(SKU, MAC_WIRE)
    counter = sealed[:4]
    assert int.from_bytes(counter, "big") == 1
    assert gc._gcm_decrypt(device_key, _client_iv_key(request) + counter, sealed[4:], counter) == frame
    assert len(sealed) == 4 + 20 + gc.TAG_LEN


def test_counter_increments_per_frame():
    session, _ = _open_session()

    counters = [int.from_bytes(session.seal(b"\0" * 20)[:4], "big") for _ in range(3)]

    assert counters == [1, 2, 3]


def test_tampered_reply_is_rejected():
    request = gc.EncryptedSession().handshake_request()
    reply = bytearray(_device_reply(request))
    reply[-1] ^= 1

    with pytest.raises(gc.GoveeCryptoError):
        gc.EncryptedSession().accept_handshake_response(bytes(reply))


def test_refused_negotiation_is_reported():
    request = gc.EncryptedSession().handshake_request()

    with pytest.raises(gc.GoveeCryptoError, match="refused"):
        gc.EncryptedSession().accept_handshake_response(_device_reply(request, status=1))


def test_seal_requires_handshake():
    with pytest.raises(gc.GoveeCryptoError):
        gc.EncryptedSession().seal(b"\0" * 20)


@pytest.mark.parametrize("marker,version", [(None, 0), (b"", 0), (bytes.fromhex("0102") + bytes(18), 2),
                                            (bytes.fromhex("0101"), 1), (bytes.fromhex("0900"), 0)])
def test_parse_encryption_version(marker, version):
    assert gc.parse_encryption_version(marker) == version
