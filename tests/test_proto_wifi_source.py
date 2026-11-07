"""proto.c's WiFi link-source gates and frame rules, pinned from the source
(proto.c cannot be host-built; see tests/test_ota_wifi_source.py).

Two lines of proto.c decide the WiFi security model and nothing else tests
either: scan, set and forget act only on a line from the UART, and only a
UART line counts as a USB host. drain_wifi's rules keep the WiFi path in
step with the UART's drop-whole rule (CLAUDE.md, the wire protocol).
"""
import pathlib
import re

FW = pathlib.Path(__file__).resolve().parents[1] / "firmware" / "src"


def _strip(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


PROTO = _strip((FW / "proto.c").read_text(encoding="utf-8"))
SERVER = _strip((FW / "wl_server.c").read_text(encoding="utf-8"))


def _fn(src, head):
    m = re.search(head + r".*?\n\}", src, re.S)
    assert m, head
    return m.group(0)


def test_wifi_commands_act_only_on_a_uart_line():
    m = re.search(r'strncmp\(type, "wifi_", 5\) == 0\) \{(.*?)\n\t\} else if',
                  PROTO, re.S)
    assert m
    body = re.sub(r"\s+", " ", m.group(1)).strip()
    assert body == "if (src == PROTO_SRC_USB) { wifi_handle(type, json); }"
    # And nothing else calls it: a second caller would be an ungated route.
    assert len(re.findall(r"\bwifi_handle\(", PROTO)) == 2   # definition, call


def test_only_a_uart_line_counts_as_a_usb_host():
    d = _fn(PROTO, r"static void dispatch\(")
    calls = re.findall(r"link_usb_heard\(", d)
    assert len(calls) == 1
    assert re.search(r"if \(src == PROTO_SRC_USB\) \{\s*link_usb_heard\(", d)
    assert len(re.findall(r"link_usb_heard\(", PROTO)) == 1


def test_the_two_line_sources_are_the_two_call_sites_of_dispatch():
    assert re.findall(r"\bdispatch\(.*?, (PROTO_SRC_\w+)\);", PROTO) == [
        "PROTO_SRC_USB", "PROTO_SRC_WIFI"]


def _drain():
    return _fn(PROTO, r"static void drain_wifi\(void\)")


def test_a_wifi_line_with_a_newline_inside_is_refused_whole():
    d = _drain()
    i = d.index("memchr(body, '\\n', (size_t)n - 1)")
    assert re.match(r"[^;]*\)\s*\{\s*continue;", d[i:]), "refusal must skip the frame"
    # Before the trailing newlines are trimmed, so a newline in the middle
    # is seen: trimming first would let "a\nb\n" through as "a\nb".
    assert i < d.index("body[n - 1] == '\\n'")


def test_a_wifi_line_at_or_over_the_uart_limit_is_dropped():
    d = _drain()
    assert "n > 0 && n < LINE_MAX" in d
    assert d.index("n < LINE_MAX") < d.index("dispatch((const char *)body")
    assert re.search(r"#define LINE_MAX 512", PROTO)


def test_blob_frames_go_to_the_sink_which_closes_the_session_without_a_handler():
    d = _drain()
    assert re.search(r"if \(type != WL_T_LINE\) \{\s*wl_server_sink\(type, body, \(size_t\)n\);\s*continue;", d)
    s = _fn(SERVER, r"void wl_server_sink\(")
    assert re.search(r"if \(fn != NULL\) \{\s*fn\(body, n\);\s*\} else \{\s*wl_server_close_session\(\);", s)


def test_a_failed_slot_is_skipped_not_dispatched():
    d = _drain()
    assert re.search(r"if \(n == -EAGAIN\) \{\s*return;\s*\}\s*if \(n < 0\) \{\s*continue;", d)
    assert "-EMSGSIZE" in SERVER
