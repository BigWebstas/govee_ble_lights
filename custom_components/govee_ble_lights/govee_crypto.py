"""AES-GCM framing for Govee lights that report encryption version 2 on GATT 2b12.

Newer Govee lights ignore the plain 20-byte frames and only act on the same frames wrapped in
AES-128-GCM after a one-round handshake. Both keys below are app-global constants shipped in
the Govee Home APK, not per-device secrets; the per-device key is derived from the SKU and BLE
address the light itself reports during the handshake. The scheme is documented in
PROTOCOL.md of https://github.com/Fredde87/ha-govee-led-ble (MIT) and was checked against a
Govee Home capture of an H3501.

Pure functions and a small session object: no Bluetooth, no Home Assistant, no I/O.
"""

from __future__ import annotations

import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY_HANDSHAKE = bytes.fromhex("FC03783C7C42CB83E202A1643648AFF6")
KEY_DEVICE = bytes.fromhex("AE028B630BAE6ECC4BFF1B249E22F955")

MAGIC = 0xE7
CMD_SESSION = 0x11
# The Android app negotiates 12-byte tags and the light accepts them; the handshake carries the length.
TAG_LEN = 12
ENCRYPTION_VERSION_AES_GCM = 2


class GoveeCryptoError(Exception):
    """The light's reply could not be authenticated or parsed."""


def parse_encryption_version(marker: bytes | None) -> int:
    """Read the version byte from the 2b12 characteristic (`01 <version> ...`); 0 means plain."""
    if not marker or len(marker) < 2 or marker[0] not in (1, 2):
        return 0
    return marker[1]


def _gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(aad)
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return ciphertext + encryptor.tag[:TAG_LEN]


def _gcm_decrypt(key: bytes, nonce: bytes, sealed: bytes, aad: bytes) -> bytes:
    if len(sealed) < TAG_LEN:
        raise GoveeCryptoError("frame too short")
    ciphertext, tag = sealed[:-TAG_LEN], sealed[-TAG_LEN:]
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag, min_tag_length=TAG_LEN)).decryptor()
    decryptor.authenticate_additional_data(aad)
    try:
        return decryptor.update(ciphertext) + decryptor.finalize()
    except Exception as err:  # InvalidTag
        raise GoveeCryptoError("frame failed authentication") from err


def derive_device_key(sku: bytes, mac_wire_order: bytes) -> bytes:
    """AES-ECB(KEY_DEVICE, SKU || MAC zero-padded to 16); the MAC stays in the order sent on the wire."""
    block = (sku + mac_wire_order).ljust(16, b"\0")
    if len(block) != 16:
        raise GoveeCryptoError("device info too long")
    encryptor = Cipher(algorithms.AES(KEY_DEVICE), modes.ECB()).encryptor()  # noqa: S305
    return encryptor.update(block) + encryptor.finalize()


class EncryptedSession:
    """One connection's key exchange and frame sealing.

    Flow: write `handshake_request()`, feed the light's 0xE711 reply to `accept_handshake_response()`,
    then wrap every 20-byte command frame with `seal()`.
    """

    def __init__(self) -> None:
        self._client_iv_key = os.urandom(8)
        self._device_key: bytes | None = None
        self._counter = 1

    def handshake_request(self) -> bytes:
        """`E7 11 01 <iv 12> <tagLen> gcm(client ivKey)`; AAD is the frame's first 16 bytes."""
        iv = os.urandom(12)
        header = bytes([MAGIC, CMD_SESSION, 0x01]) + iv + bytes([TAG_LEN])
        return header + _gcm_encrypt(KEY_HANDSHAKE, iv, self._client_iv_key, header)

    @staticmethod
    def is_handshake_response(frame: bytes) -> bool:
        return len(frame) >= 2 and frame[0] == MAGIC and frame[1] == CMD_SESSION

    def accept_handshake_response(self, frame: bytes) -> None:
        """`E7 11 <status> <iv 12> gcm(device ivKey 8 | SKU 5 | MAC 6)`; AAD is the first 15 bytes."""
        frame = bytes(frame)
        if not self.is_handshake_response(frame) or len(frame) < 15 + TAG_LEN:
            raise GoveeCryptoError(f"not a handshake response: {frame[:4].hex()}")
        if frame[2] != 0x00:
            raise GoveeCryptoError(f"light refused key negotiation: status {frame[2]:#04x}")
        plain = _gcm_decrypt(KEY_HANDSHAKE, frame[3:15], frame[15:], frame[:15])
        if len(plain) != 19:
            raise GoveeCryptoError(f"unexpected handshake payload length {len(plain)}")
        self._device_key = derive_device_key(plain[8:13], plain[13:19])

    def seal(self, frame: bytes) -> bytes:
        """`counter(4, big-endian) + gcm(frame)`; nonce is the client ivKey plus the counter."""
        if self._device_key is None:
            raise GoveeCryptoError("handshake not completed")
        counter = struct.pack(">I", self._counter)
        self._counter += 1
        return counter + _gcm_encrypt(self._device_key, self._client_iv_key + counter, bytes(frame), counter)
