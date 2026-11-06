"""ChaCha20-Poly1305 (RFC 8439) and HKDF-SHA256 (RFC 5869), standard library only.

The WiFi transport seals every frame with this. It is written out here, not
imported, because pc/ ships frozen to customers and takes no runtime
dependency (CLAUDE.md): `cryptography` once cost a third of the download for
one eFuse bit. tests/pc/test_wifi_crypto.py holds it to the RFC's own vectors
and to the property that matters more than speed, that a frame whose tag does
not verify is never decrypted.

Speed is not a goal. Frames are at most 512 bytes and arrive every few
seconds, so the block function is the plain RFC text.
"""
from __future__ import annotations

import hashlib
import hmac
import struct

_M32 = 0xFFFFFFFF
_SIGMA = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)


class AuthError(ValueError):
    """The tag did not verify. The text is fixed on purpose: nothing about the
    frame, the key or the tag may reach a log through an exception message."""

    def __init__(self):
        super().__init__("frame failed authentication")


def _rotl(v, n):
    return ((v << n) & _M32) | (v >> (32 - n))


def _qr(s, a, b, c, d):
    s[a] = (s[a] + s[b]) & _M32; s[d] = _rotl(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & _M32; s[b] = _rotl(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & _M32; s[d] = _rotl(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & _M32; s[b] = _rotl(s[b] ^ s[c], 7)


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    """RFC 8439 2.3: one 64-byte keystream block."""
    if len(key) != 32 or len(nonce) != 12:
        raise ValueError("chacha20 wants a 32-byte key and a 12-byte nonce")
    init = list(_SIGMA) + list(struct.unpack("<8L", key)) \
        + [counter & _M32] + list(struct.unpack("<3L", nonce))
    s = init[:]
    for _ in range(10):
        _qr(s, 0, 4, 8, 12); _qr(s, 1, 5, 9, 13)
        _qr(s, 2, 6, 10, 14); _qr(s, 3, 7, 11, 15)
        _qr(s, 0, 5, 10, 15); _qr(s, 1, 6, 11, 12)
        _qr(s, 2, 7, 8, 13); _qr(s, 3, 4, 9, 14)
    return struct.pack("<16L", *[(a + b) & _M32 for a, b in zip(s, init)])


def chacha20_xor(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    """RFC 8439 2.4: encrypt or decrypt (the same operation)."""
    out = bytearray()
    for i in range(0, len(data), 64):
        block = chacha20_block(key, counter + i // 64, nonce)
        chunk = data[i:i + 64]
        out += bytes(a ^ b for a, b in zip(chunk, block))
    return bytes(out)


def poly1305(key: bytes, msg: bytes) -> bytes:
    """RFC 8439 2.5: the one-time authenticator."""
    if len(key) != 32:
        raise ValueError("poly1305 wants a 32-byte key")
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    p = (1 << 130) - 5
    acc = 0
    for i in range(0, len(msg), 16):
        n = int.from_bytes(msg[i:i + 16] + b"\x01", "little")
        acc = (acc + n) * r % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(b: bytes) -> bytes:
    return b"\x00" * (-len(b) % 16)


def _tag(key, nonce, aad, ct):
    otk = chacha20_block(key, 0, nonce)[:32]
    mac = aad + _pad16(aad) + ct + _pad16(ct) \
        + struct.pack("<QQ", len(aad), len(ct))
    return poly1305(otk, mac)


def aead_seal(key: bytes, nonce: bytes, aad: bytes, pt: bytes):
    """RFC 8439 2.8: (ciphertext, tag)."""
    ct = chacha20_xor(key, 1, nonce, pt)
    return ct, _tag(key, nonce, aad, ct)


def aead_open(key: bytes, nonce: bytes, aad: bytes, ct: bytes,
              tag: bytes) -> bytes:
    """The plaintext, or AuthError. The tag is compared, in constant time,
    before chacha20_xor is called at all: a forged frame is never decrypted."""
    if not hmac.compare_digest(_tag(key, nonce, aad, ct), tag):
        raise AuthError()
    return chacha20_xor(key, 1, nonce, ct)


def hkdf_sha256(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 extract-and-expand with SHA-256."""
    if length > 255 * 32:
        raise ValueError("hkdf output too long")
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm, t = b"", b""
    for i in range(1, -(-length // 32) + 1):
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
    return okm[:length]
