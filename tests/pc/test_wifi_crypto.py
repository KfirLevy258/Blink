"""RFC 8439 and RFC 5869 vectors for pc/wifi_crypto.py, plus the properties
the WiFi transport leans on: a forged frame is rejected before anything is
decrypted, and the error says nothing about the data."""
import ast
import pathlib

import pytest

from pc import wifi_crypto as wc

KEY00 = bytes(range(32))
LADIES = (b"Ladies and Gentlemen of the class of '99: If I could offer you "
          b"only one tip for the future, sunscreen would be it.")


def test_rfc8439_2_3_2_block():
    """RFC 8439 section 2.3.2, the block function test vector."""
    out = wc.chacha20_block(KEY00, 1, bytes.fromhex("000000090000004a00000000"))
    assert out.hex() == (
        "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
        "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e")


def test_rfc8439_2_4_2_encrypt():
    """RFC 8439 section 2.4.2, the sunscreen ciphertext."""
    ct = wc.chacha20_xor(KEY00, 1, bytes.fromhex("000000000000004a00000000"),
                         LADIES)
    assert ct.hex() == (
        "6e2e359a2568f98041ba0728dd0d6981e97e7aec1d4360c20a27afccfd9fae0b"
        "f91b65c5524733ab8f593dabcd62b3571639d624e65152ab8f530c359f0861d8"
        "07ca0dbf500d6a6156a38e088a22b65e52bc514d16ccf806818ce91ab7793736"
        "5af90bbf74a35be6b40b8eedf2785e42874d")


def test_rfc8439_2_5_2_poly1305():
    """RFC 8439 section 2.5.2, the Poly1305 test vector."""
    key = bytes.fromhex("85d6be7857556d337f4452fe42d506a8"
                        "0103808afb0db2fd4abff6af4149f51b")
    tag = wc.poly1305(key, b"Cryptographic Forum Research Group")
    assert tag.hex() == "a8061dc1305136c6c22b8baf0c0127a9"


AEAD_KEY = bytes(range(0x80, 0xA0))
AEAD_NONCE = bytes.fromhex("070000004041424344454647")
AEAD_AAD = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
AEAD_CT = (
    "d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
    "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
    "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
    "3ff4def08e4b7a9de576d26586cec64b6116")
AEAD_TAG = "1ae10b594f09e26a7e902ecbd0600691"


def test_rfc8439_2_8_2_aead_seal():
    """RFC 8439 section 2.8.2, the AEAD construction."""
    ct, tag = wc.aead_seal(AEAD_KEY, AEAD_NONCE, AEAD_AAD, LADIES)
    assert ct.hex() == AEAD_CT
    assert tag.hex() == AEAD_TAG


A5_KEY = bytes.fromhex("1c9240a5eb55d38af333888604f6b5f0"
                       "473917c1402b80099dca5cbc207075c0")
A5_NONCE = bytes.fromhex("000000000102030405060708")
A5_AAD = bytes.fromhex("f33388860000000000004e91")
A5_PT = (b"Internet-Drafts are draft documents valid for a maximum of six "
         b"months and may be updated, replaced, or obsoleted by other "
         b"documents at any time. It is inappropriate to use Internet-Drafts"
         b" as reference material or to cite them other than as "
         b"/\xe2\x80\x9cwork in progress./\xe2\x80\x9d")
# The tag is the RFC's own (A.5); the ciphertext was produced by an
# independent implementation for the same inputs and agrees with the RFC's
# printed first bytes and with this tag, which covers every byte of it.
A5_CT = (
    "64a0861575861af460f062c79be643bd5e805cfd345cf389f108670ac76c8cb2"
    "4c6cfc18755d43eea09ee94e382d26b0bdb7b73c321b0100d4f03b7f355894cf"
    "332f830e710b97ce98c8a84abd0b948114ad176e008d33bd60f982b1ff37c855"
    "9797a06ef4f0ef61c186324e2b3506383606907b6a7c02b0f9f6157b53c867e4"
    "b9166c767b804d46a59b5216cde7a4e99040c5a40433225ee282a1b0a06c523e"
    "af4534d7f83fa1155b0047718cbc546a0d072b04b3564eea1b422273f548271a"
    "0bb2316053fa76991955ebd63159434ecebb4e466dae5a1073a6727627097a10"
    "49e617d91d361094fa68f0ff77987130305beaba2eda04df997b714d6c6f2c29"
    "a6ad5cb4022b02709b")
A5_TAG = "eead9d67890cbb22392336fea1851f38"


def test_rfc8439_a5_aead_open():
    """RFC 8439 appendix A.5, the decryption test vector."""
    assert len(A5_PT) == 265
    got = wc.aead_open(A5_KEY, A5_NONCE, A5_AAD, bytes.fromhex(A5_CT),
                       bytes.fromhex(A5_TAG))
    assert got == A5_PT


@pytest.mark.parametrize("n", [0, 1, 63, 64, 65, 511, 512, 517])
def test_boundary_lengths_round_trip(n):
    pt = bytes((i * 7) & 0xFF for i in range(n))
    ct, tag = wc.aead_seal(KEY00, bytes(12), b"hdr", pt)
    assert len(ct) == n and len(tag) == 16
    assert wc.aead_open(KEY00, bytes(12), b"hdr", ct, tag) == pt


@pytest.mark.parametrize("n", [1, 517])
def test_a_flipped_bit_anywhere_fails(n):
    aad = b"" * (n and 1)
    pt = bytes(range(256)) * 3
    pt = pt[:n]
    ct, tag = wc.aead_seal(KEY00, bytes(12), aad, pt)
    parts = {"ct": ct, "tag": tag, "aad": aad}
    for name, data in parts.items():
        for bit in range(len(data) * 8):
            bad = bytearray(data)
            bad[bit // 8] ^= 1 << (bit % 8)
            args = dict(parts, **{name: bytes(bad)})
            with pytest.raises(wc.AuthError):
                wc.aead_open(KEY00, bytes(12), args["aad"], args["ct"],
                             args["tag"])


def test_open_never_decrypts_a_forged_frame(monkeypatch):
    ct, tag = wc.aead_seal(KEY00, bytes(12), b"", b"hello")
    calls = []
    real = wc.chacha20_xor
    monkeypatch.setattr(wc, "chacha20_xor",
                        lambda *a: calls.append(a) or real(*a))
    with pytest.raises(wc.AuthError):
        wc.aead_open(KEY00, bytes(12), b"", ct, bytes(16))
    assert calls == []
    assert wc.aead_open(KEY00, bytes(12), b"", ct, tag) == b"hello"
    assert len(calls) == 1


def test_auth_error_says_nothing_about_the_data():
    secret = b"SECRET-PLAINTEXT"
    ct, tag = wc.aead_seal(KEY00, bytes(12), b"aad-SECRET", secret)
    with pytest.raises(wc.AuthError) as e:
        wc.aead_open(KEY00, bytes(12), b"aad-SECRET", ct, bytes(16))
    assert str(e.value) == "frame failed authentication"
    assert e.value.args == ("frame failed authentication",)
    assert isinstance(e.value, ValueError)


def test_rfc5869_case_1():
    """RFC 5869 appendix A.1, basic SHA-256."""
    okm = wc.hkdf_sha256(bytes(range(13)), b"" * 22,
                         bytes(range(0xF0, 0xFA)), 42)
    assert okm.hex() == ("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c"
                         "5db02d56ecc4c5bf34007208d5b887185865")


def test_rfc5869_case_2():
    """RFC 5869 appendix A.2, longer inputs and outputs."""
    okm = wc.hkdf_sha256(bytes(range(0x60, 0xB0)), bytes(range(0x50)),
                         bytes(range(0xB0, 0x100)), 82)
    assert okm.hex() == (
        "b11e398dc80327a1c8e7f78c596a49344f012eda2d4efad8a050cc4c19afa97c"
        "59045a99cac7827271cb41c65e590e09da3275600c2f09b8367793a9aca3db71"
        "cc30c58179ec3e87c14c01d5c1f3434f1d87")


def test_rfc5869_case_3():
    """RFC 5869 appendix A.3, empty salt and info."""
    okm = wc.hkdf_sha256(b"", b"" * 22, b"", 42)
    assert okm.hex() == ("8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879e"
                         "c3454e5f3c738d2d9d201395faa4b61a96c8")


def test_stdlib_only():
    tree = ast.parse(pathlib.Path(wc.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module.split(".")[0])
    assert mods <= {"__future__", "hmac", "hashlib", "struct"}
