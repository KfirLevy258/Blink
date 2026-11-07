"""Every reason the board can send in a wifi_state `why` is on the debug
transcript's allowlist (protocol.TAP_ENUMS). The list is hand-kept, and the
round-2 review of PR 9 found it two sentences behind net_link_why_text():
with BLINK_TAP on, a refused join or scan lost its reason in the transcript."""
import re
from pathlib import Path

from pc import protocol

ROOT = Path(__file__).resolve().parents[2]


def test_every_firmware_why_text_is_allowlisted():
    src = (ROOT / "firmware" / "src" / "net_link.c").read_text()
    body = src[src.index("const char *net_link_why_text"):]
    body = body[:body.index("\n}\n")]
    texts = set(re.findall(r'return "([^"]*)";', body))
    assert len(texts) >= 9  # eight reasons and the empty default
    assert texts <= protocol.TAP_ENUMS["why"], texts - protocol.TAP_ENUMS["why"]
