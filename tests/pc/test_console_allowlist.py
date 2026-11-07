"""The board's console reaches bridge.log as a tag and a fixed message, and
nothing the firmware formatted into the line.

pc/logbook.ConsoleEcho used to forward each printk line verbatim: the usage
the board applied, every version on offer, the host's app and protocol, heap
figures, touch coordinates, the edition. These tests feed it lines rendered
the way the firmware renders them, with marker values, and check that only
the allowlisted constant text comes out. They fail against c172731, which
echoed every one of these lines whole.

The second half keeps the allowlist honest against firmware/src: an entry
that stops being the start of a real printk, or a tag the firmware starts
using that the list does not know, fails here instead of quietly turning a
support-relevant line into a bare tag.
"""
import json
import pathlib
import random
import re

import pytest

from pc import logbook

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "firmware" / "src"


# --- what reaches the log ----------------------------------------------------

# (line as the board prints it, what the log may say, values that must not
# appear). The formats are the firmware's, cited by file:line.
LINES = [
    # proto.c:627 -- the usage the board applied
    ("[usage] session 41% (11723s)  weekly 9% (549923s)  STALE",
     "[usage] session", ["41", "11723", "549923", "STALE"]),
    # ota.c:412
    ("[ota] downloading 7.7.7 (1298765 bytes)", "[ota] downloading",
     ["7.7.7", "1298765"]),
    # ota.c:374 -- not allowlisted: its value is the whole point
    ("[ota] latest 7.7.7 (running 6.6.6) -> install", "[ota]",
     ["7.7.7", "6.6.6", "install"]),
    # main.c:233 -- one of two words the firmware picks, kept; the rest cut
    ("[ota] update REVERTED: wanted 7.7.7, running 6.6.6 (from 6.6.6)",
     "[ota] update REVERTED", ["7.7.7", "6.6.6"]),
    ("[ota] update landed: wanted 7.7.7, running 7.7.7 (from 6.6.6)",
     "[ota] update landed", ["7.7.7", "6.6.6"]),
    # main.c:126
    ("[ota] test boot of 7.7.7 -- must confirm within 90 s",
     "[ota] test boot of", ["7.7.7", "must confirm"]),
    # main.c:160, a fixed message, kept whole
    ("[ota] image confirmed", "[ota] image confirmed", []),
    # proto.c:727
    ("[proto] host connected: app 7.7.7, protocol 9", "[proto] host connected",
     ["7.7.7", "protocol 9"]),
    # proto.c:889 -- the why is the daemon's text echoed back
    ("[proto] ota failed: download failed: HTTP 503 at cdn.example",
     "[proto] ota failed", ["503", "cdn.example"]),
    # fatal.c:68
    ("[fatal] reason 4, up 31337 s: rebooting, fault reboot 2 of 3",
     "[fatal] reason", ["31337", "rebooting"]),
    # bootclip.c:68
    ("[boot] edition: codex", "[boot] edition", ["codex"]),
    # logo.c:46
    ("[boot] logo: company, 48213 bytes, 12 frames, hold 1500 ms",
     "[boot] logo: company", ["48213", "1500"]),
    # cfg_store.c:491 -- older firmware printed the network name here
    ("[cfg] mode=1 ssid=home-net-SECRET token=present", "[cfg]",
     ["home-net-SECRET", "ssid", "token"]),
    # proto.c:698
    ("[cfg] edition set to codex (applies on next boot)",
     "[cfg] edition set to", ["codex"]),
    # mem_stats.c:131
    ("[mem] lvgl settings: used=31337 free=2320 largest=1999 peak=30001"
     " boot_peak=29000 pool=32768", "[mem]", ["31337", "2320", "settings"]),
    # ui_swipe.c:313, backlight.c:39
    ("[swipe] dx 120 dy -3 in 87 ms, 14 samples -> 1", "[swipe]", ["120"]),
    ("[bl] 73%", "[bl]", ["73"]),
    # ui_sleep.c:183
    ("[sleep] dozing (night)", "[sleep] dozing", ["night"]),
    # net_link.c -- the radio's four fixed lines (spec S5.8). The board never
    # prints a network name, but a line that carried one must still lose it.
    ("[wifi] radio up", "[wifi] radio up", []),
    ("[wifi] joined MyNet", "[wifi] joined", ["MyNet"]),
    ("[wifi] join failed: Wrong WiFi password", "[wifi] join failed",
     ["Wrong", "password"]),
    ("[wifi] lost the network", "[wifi] lost the network", []),
    ("[wifi] paired", "[wifi] paired", []),
    ("[wifi] forgotten MyNet", "[wifi] forgotten", ["MyNet"]),
    ("[wifi] forget failed", "[wifi] forget failed", []),
    # wl_server.c -- the session lines; a peer's address riding on one is cut
    ("[wifi] session open", "[wifi] session open", []),
    ("[wifi] session closed 192.168.1.9", "[wifi] session closed",
     ["192.168.1.9"]),
    ("[wifi] handshake refused: bad tag", "[wifi] handshake refused",
     ["bad", "tag"]),
    ("[wifi] verified", "[wifi] verified", []),
    ("[wifi] heap low, restarting", "[wifi] heap low, restarting", []),
    ("[wifi] heap free=31244", "[wifi] heap free", ["31244"]),
    ("[logo] remove failed: -5", "[logo] remove failed", ["-5"]),
    ("[wifi] listen failed: -23", "[wifi] listen failed", ["-23"]),
    ("[wifi] discovery socket failed: -23", "[wifi] discovery socket failed",
     ["-23"]),
    ("[wifi] scan failed: -16", "[wifi] scan failed", ["-16"]),
    # a known stem with something riding on the end of it
    ("[theme] saved SECRET-TAIL", "[theme] saved", ["SECRET-TAIL"]),
    # no tag the firmware uses: the bootloader, Zephyr, the touch tracer,
    # line noise, or a tag nobody prints
    ("*** Booting Zephyr OS build v4.3.0 ***", "*** Booting Zephyr OS",
     ["4.3.0"]),
    ("[esp32] [INF] Image index: 0, Swap type: none", logbook.BOARD_LINE,
     ["esp32", "Swap"]),
    ("#TT 12,3456,100,200,1", logbook.BOARD_LINE, ["3456"]),
    ("[a1b2c3d4] SECRET", logbook.BOARD_LINE, ["a1b2c3d4", "SECRET"]),
]


@pytest.mark.parametrize("line,want,secrets", LINES,
                         ids=[l[0][:40] for l in LINES])
def test_a_board_line_is_logged_as_its_constant_text(line, want, secrets):
    out = logbook.ConsoleEcho().feed((line + "\r\n").encode())
    assert out == [want]
    for s in secrets:
        assert s not in out[0], s


@pytest.mark.parametrize("line", ["[proto] burn line refused",   # proto.c:995
                                  "[proto] sburn line refused",  # proto.c:993
                                  "[theme] line refused"])       # proto.c:769
def test_a_refused_line_is_named_in_the_log(line):
    """The board refusing a line the daemon sent is the one piece of
    evidence of a contract that moved, and it carries no value, so it is
    kept whole. The burn view's refusal was left off the list when the burn
    line was added, and reached bridge.log as a bare "[proto]"."""
    assert logbook.ConsoleEcho().feed((line + "\r\n").encode()) == [line]


def test_no_figure_from_a_boot_reaches_the_log():
    """A whole boot's worth at once, the way the read loop sees it."""
    boot = "".join(l + "\n" for l, _, _ in LINES)
    out = "\n".join(logbook.ConsoleEcho().feed(boot.encode()))
    for _, _, secrets in LINES:
        for s in secrets:
            assert s not in out, s


# --- the allowlist against the firmware ---------------------------------------

_LITERAL = r'"(?:[^"\\]|\\.)*"'
_PRINTK = re.compile(r'\bprintk\(\s*((?:' + _LITERAL + r'\s*)+)', re.S)


def _formats():
    """Every printk format string in firmware/src, C's adjacent literals
    joined, with the file it came from."""
    out = []
    for path in sorted(SRC.rglob("*.[ch]")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _PRINTK.finditer(text):
            fmt = "".join(s[1:-1] for s in re.findall(_LITERAL, m.group(1)))
            out.append((path.name, fmt))
    return out


def test_the_scan_finds_the_firmware_prints():
    # Fewer than this and the regex has gone blind, which would make every
    # test below pass vacuously.
    assert len(_formats()) > 80


@pytest.mark.parametrize("stem", logbook.CONSOLE_MESSAGES)
def test_every_allowlisted_message_is_still_a_firmware_message(stem):
    """The stem is the start of a real printk, holds no format of its own,
    and ends where a word does -- so it is the constant text before a value,
    or the whole message."""
    assert "%" not in stem
    for _name, fmt in _formats():
        if fmt.startswith(stem):
            rest = fmt[len(stem):]
            if rest == "" or rest.startswith("\\n") or not rest[0].isalnum():
                return
    pytest.fail(f"no printk in firmware/src starts with {stem!r}; update "
                f"pc/logbook.CONSOLE_MESSAGES to match the firmware")


def test_the_two_update_outcomes_are_still_the_firmwares_own_words():
    main = (SRC / "main.c").read_text(encoding="utf-8")
    assert any(fmt.startswith("[ota] update %s:") for _n, fmt in _formats())
    for word in ("landed", "REVERTED"):
        assert f'"{word}"' in main, word
    assert set(logbook.CONSOLE_RENDERED) == {"[ota] update landed",
                                             "[ota] update REVERTED"}


def test_the_tag_set_is_exactly_the_firmwares():
    used = set()
    for _name, fmt in _formats():
        m = re.match(r"\\?n?\[([a-z_]+)\]", fmt)
        if m:
            used.add(m.group(1))
    assert used == set(logbook.CONSOLE_TAGS), (
        "firmware/src printk tags and pc/logbook.CONSOLE_TAGS differ: "
        f"only in firmware {sorted(used - logbook.CONSOLE_TAGS)}, "
        f"only in the list {sorted(logbook.CONSOLE_TAGS - used)}")


# --- the lines that are not ours (CONSOLE_EXTERNAL, D49) ----------------------

@pytest.mark.parametrize("stem", logbook.CONSOLE_EXTERNAL)
def test_external_stems_are_their_own_list(stem):
    assert logbook.console_line(stem) == stem
    assert logbook.console_line(stem + ",boot:0x13 (SPI_FAST_FLASH_BOOT)"
                                ) == stem
    assert stem not in logbook.CONSOLE_MESSAGES


def test_external_list_keeps_the_roms_reset_reasons():
    line = "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)"
    assert logbook.console_line(line) == "rst:0x1 (POWERON_RESET)"
    assert not set(logbook.CONSOLE_EXTERNAL) & set(logbook.CONSOLE_MESSAGES)


# --- the transcript allowlist (tap_console_line) -----------------------------

TAP_LINES = [
    ('{"t":"usage","session_pct":41}', '{"t":"usage","session_pct":41}'),
    ('{"t":"ping","k":"SECRET"}', '{"t":"ping"}'),
    ('{"t":"wifi_state","st":"up","s":"6e6574","ip":"192.168.1.2"}',
     '{"t":"wifi_state","st":"up"}'),
    ('{"t":"wifi_stat","s":"6e6574"}', logbook.BOARD_LINE),
    ('{"t":"wifi_state","st":"up","s":"6e6574"', logbook.BOARD_LINE),
    ('[usage] sess{"t":"wifi_state","s":"6e6574"}', "[usage]"),
    ('"s":"6e6574"', logbook.BOARD_LINE),
    # an unhashable or non-string t is not a protocol object
    ('{"t":[1]}', logbook.BOARD_LINE),
    ('{"t":{}}', logbook.BOARD_LINE),
    ('{"t":7,"s":"6e6574"}', logbook.BOARD_LINE),
    ('[1,2]', logbook.BOARD_LINE),
    ("[cfg] mode=1 ssid=home-net-SECRET token=present", "[cfg]"),
    ("[proto] ota failed: anything", "[proto] ota failed"),
    ("[usage] session 3", logbook.APPLIED_LINE),
    ("[usage] session 7 STALE", logbook.APPLIED_LINE + " STALE"),
    ("[usage] session 7 NOTSTALE", logbook.APPLIED_LINE),
    ("[usage] session 50% (12s)  weekly 4% (9s)  STALE +state",
     logbook.APPLIED_LINE + " STALE"),
    ("rst:0xf (BROWNOUT_RESET),boot:0x13", "rst:0xf (BROWNOUT_RESET)"),
    ("garbage \x00\xff 6e6574", logbook.BOARD_LINE),
]


@pytest.mark.parametrize("line,want", TAP_LINES,
                         ids=[l[0][:40] for l in TAP_LINES])
def test_tap_console_line_is_an_allowlist(line, want):
    assert logbook.tap_console_line(line) == want


# A literal copy of protocol.TAP_FIELDS, so a field added there without a
# decision here fails this test rather than passing it.
_FIELDS = {
    "hello": "t", "ping": "t", "pref": "t", "ota_query": "t",
    "ota_flash": "t", "theme_now": "t", "widgets": "t",
    "theme_edit": "t ok done", "touch_done": "t op ok", "touch_refused": "t",
    "ui_state": "t scr ov modal busy theme stored tp focus bl edit slp nt gf",
    "wifi_net": "t i n sec", "wifi_state": "t st link kid why",
    "ota_ack": "t off err", "logo_ack": "t", "logo_state": "t",
    "welcome": "t app app_ver editing", "bye": "t", "pong": "t",
    "time": "t epoch utc_offset_min",
    "usage": "t session_pct session_resets_in_s weekly_pct "
             "weekly_resets_in_s stale fable_pct sonnet_pct opus_pct "
             "provider src state n_sess n_run n_wait n_stuck n_agents p2 "
             "p2_session_pct p2_weekly_pct p2_s_in_s p2_w_in_s p2_stale "
             "burn_pph age_s p2_age_s active_age_s",
    "edition": "t edition", "session": "t n", "status": "t state",
    "svc": "t provider level", "theme": "t op id",
    "ota_avail": "t version size", "ota_begin": "t version",
    "ota_resume": "t version", "ota_none": "t", "ota_error": "t",
    "edit_state": "t editing", "wifi_scan": "t", "wifi_set": "t",
    "wifi_forget": "t", "ota_end": "t", "logo_begin": "t", "logo_chunk": "t",
    "logo_end": "t", "logo_remove": "t", "pref_set": "t",
    "verify": "t", "verified": "t",
}


def _allowed(out):
    if out == logbook.BOARD_LINE or out in (
            logbook.CONSOLE_RENDERED + logbook.CONSOLE_MESSAGES
            + logbook.CONSOLE_EXTERNAL):
        return True
    if out in (logbook.APPLIED_LINE, logbook.APPLIED_LINE + " STALE"):
        return True
    m = re.fullmatch(r"\[([a-z_]+)\]", out)
    if m and m.group(1) in logbook.CONSOLE_TAGS:
        return True
    obj = json.loads(out)       # anything else must be redacted JSON
    assert obj["t"] in _FIELDS, out
    assert set(obj) <= set(_FIELDS[obj["t"]].split()), out
    return all(v is None or isinstance(v, (str, int, float, bool))
               for v in obj.values())


def test_every_output_is_in_the_allowlist_or_redacted_json():
    rng = random.Random(20261106)
    for _ in range(10000):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 80)))
        out = logbook.tap_console_line(raw.decode("utf-8", errors="replace"))
        assert _allowed(out), out
    pool = ["s", "p", "k", "ip", "port", "r", "x", "kid", "why", "st", "link",
            "sec", "v", "i", "n", "err", "off", "label", "name"]
    values = ["6e6574", "192.168.1.2", "home-net-SECRET", 7, -50, 2.5, None,
              True, {"nested": "SECRET"}, ["SECRET"], "x" * 90, "up", "wifi",
              "0a1b2c3d", ""]
    types = sorted(_FIELDS) + ["wifi_stat", "x"]
    for _ in range(1000):
        obj = {k: rng.choice(values)
               for k in rng.sample(pool, rng.randrange(0, 8))}
        obj["t"] = rng.choice(types + [[1], {}, 7, None, True, 2.5])
        out = logbook.tap_console_line(json.dumps(obj))
        assert _allowed(out), out
        assert "nested" not in out
