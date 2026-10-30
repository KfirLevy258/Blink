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
import pathlib
import re

import pytest

from pc import logbook

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "firmware" / "src"


# --- what reaches the log ----------------------------------------------------

# (line as the board prints it, what the log may say, values that must not
# appear). The formats are the firmware's, cited by file:line.
LINES = [
    # proto.c:620 -- the usage the board applied
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
    # proto.c:711
    ("[proto] host connected: app 7.7.7, protocol 9", "[proto] host connected",
     ["7.7.7", "protocol 9"]),
    # proto.c:860 -- the why is the daemon's text echoed back
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
    # proto.c:688
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
    # a known stem with something riding on the end of it
    ("[theme] saved SECRET-TAIL", "[theme] saved", ["SECRET-TAIL"]),
    # no tag the firmware uses: the bootloader, Zephyr, the touch tracer,
    # line noise, or a tag nobody prints
    ("*** Booting Zephyr OS build v4.3.0 ***", logbook.BOARD_LINE,
     ["Zephyr", "4.3.0"]),
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
