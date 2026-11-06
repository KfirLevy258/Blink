"""The board never opens a connection of its own (spec S9, Task 5.1).

ota.c's HTTPS half was deleted because, with a network stack in the image, it
was one linker decision away from a board that fetches from GitHub by itself,
which is the standalone mode by another name. The board listens
(wl_server.c) and the daemon connects; nothing in firmware/src may call
connect().
"""
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "firmware" / "src"
CONNECT = re.compile(r"zsock_connect|\bconnect\s*\(")


def _code(text):
    """Comments out: prose may say 'connect (' without calling it."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def test_no_firmware_source_connects():
    sources = sorted(SRC.glob("*.c"))
    assert len(sources) > 40            # the glob found the firmware
    hits = [p.name for p in sources
            if CONNECT.search(_code(p.read_text(encoding="utf-8")))]
    assert hits == []


def test_the_https_half_and_its_certificates_are_gone():
    assert not (SRC / "certs.h").exists()
    ota = _code((SRC / "ota.c").read_text(encoding="utf-8"))
    for gone in ("https_get", "fetch_follow", "tls_credential_add",
                 "http_client_req"):
        assert gone not in ota
