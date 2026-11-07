"""The weekly gauge's Focus line never wears a colour its page did not earn.

firmware/src/usage_view.c colours Focus's dim line from `weekly.shown`
(countdown_colour: amber at 60, red at 90). A page whose right gauge shows
no percentage -- the spend page's dollars, an Enterprise or Team login that
reports nothing -- has to reset `shown` to -1, or the line takes the
previous page's severity and keeps it. draw_spent() did; the unreported
page did not, so "Not reported" stayed red after a windows page at 96%,
through every tick, heartbeat and theme change (the review of the WSL work,
2026-11-05, reproduced on host LVGL with Focus on).

Read from the C, as tests/pc/test_burn_line.py reads BURN_FRESH_MS: LVGL
is not available to the host tests (tests/ci/check_host_tests.sh builds
pure logic only), and the rule is a line in each function.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "firmware" / "src" / "usage_view.c"


def _body(src, name):
    m = re.search(r"^static void " + name + r"\(void\)\n\{\n(.*?)^\}",
                  src, re.S | re.M)
    assert m, f"{name}() not found in usage_view.c"
    return m.group(1)


def test_every_right_gauge_without_a_percentage_resets_shown():
    src = SRC.read_text(encoding="utf-8")
    for name in ("draw_spent", "draw_unreported_account"):
        body = _body(src, name)
        assert "weekly.shown = -1;" in body, name
        # Before anything is drawn from it.
        assert body.index("weekly.shown = -1;") < body.index(
            "lv_label_set_text(weekly.name"), name
