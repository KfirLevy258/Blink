"""proto.c's WiFi OTA wiring, pinned from the source (it cannot be host-built).

A mid-stream ota_avail, ota_none or ota_resume must end the stream, a failed
stream must spend the offer, and ota_resume over WiFi goes through
ota_resume_honoured (tests/ota_parse), never straight to DOWNLOADING.
"""
import pathlib
import re

def _strip(text):
    """Comments out, so a commented-out line cannot satisfy a pin."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


PROTO = _strip((pathlib.Path(__file__).resolve().parents[1]
                / "firmware" / "src" / "proto.c").read_text(encoding="utf-8"))


def _branch(name):
    m = re.search(r'strcmp\(type, "%s"\) == 0\) \{(.*?)\n\t\} else if' % name,
                  PROTO, re.S)
    assert m, name
    return m.group(1)


def test_every_offer_rewriting_line_drops_an_open_stream():
    for name in ("ota_avail", "ota_none", "ota_resume"):
        assert "OTA_STREAM_DROP();" in _branch(name), name


def test_resume_over_wifi_is_conditional_on_a_tap():
    assert "ota_resume_honoured(" in _branch("ota_resume")


def test_a_failed_stream_spends_the_offer():
    body = re.search(r"static void ota_wifi_failed\(.*?\n\}", PROTO, re.S).group(0)
    assert "ota_staged = false;" in body


def test_the_drop_macro_really_drops_in_the_wifi_build():
    m = re.search(r"#define OTA_STREAM_DROP\(\) do \{ if \(ota_net_active\(\)\) "
                  r"ota_wifi_gone\(\); \} while \(0\)", PROTO)
    assert m, "the WiFi-build OTA_STREAM_DROP must call ota_wifi_gone"
    # The only empty definition is the one for a build without the WiFi link.
    empties = [x.start() for x in
               re.finditer(r"#define OTA_STREAM_DROP\(\) do \{ \} while \(0\)", PROTO)]
    assert len(empties) == 1 and empties[0] > m.start()
    between = PROTO[m.start():empties[0]]
    assert re.search(r"#else\s*$", between.rstrip() + "\n", re.M) or "#else" in between


def test_resume_asks_with_the_link_it_came_from():
    assert re.search(r"ota_resume_honoured\(\s*src == PROTO_SRC_WIFI,", _branch("ota_resume"))
