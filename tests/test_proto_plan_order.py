"""proto.c names a page before it draws it, and keeps its plan across a
reconnect until the daemon has had its say.

Nothing on the host compiles proto.c (see tests/test_proto_edit_calls.py,
which this follows), so the order of its calls is pinned by reading the
source. What each order prevents was seen on host LVGL against
usage_view.c, in the review of feat/enterprise-spend (2026-11-04); the real
proof is a board.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "firmware" / "src"
       / "proto.c").read_text(encoding="utf-8")


def _arm(t):
    """The body of dispatch()'s `strcmp(type, "t") == 0` arm: up to the next
    arm, or the end of the function."""
    m = re.search(r'strcmp\(type, "%s"\) == 0\) \{' % re.escape(t), SRC)
    assert m, f"no dispatch arm for {t!r} in proto.c"
    rest = SRC[m.end():]
    end = re.search(r'\n\t\} else if \(strcmp\(type, |\n\t/\* unknown types '
                    r'ignored \*/|\n\t\}\n\}', rest)
    assert end, f"cannot find where the {t!r} arm ends"
    return rest[:end.start()]


def test_a_usage_line_names_its_page_before_drawing_it():
    # The plan record is matched against the page's provider tag. Drawn
    # first and named after, a board that booted before the daemon showed
    # a spend share as "SESSION 5h 63%" for up to a minute (FW1).
    body = _arm("usage")
    assert body.index("usage_view_set_provider1(p1)") < \
        body.index("usage_view_update(sp,")


def test_a_welcome_leaves_the_plan_on_screen():
    # A reconnect that does not reset the board sends welcome more than
    # once -- the probe's, then greet()'s -- before greet() restates the
    # plan. Reset there, a spend page flashed as "SESSION 5h" with a red
    # "Session almost used up" on every such reconnect (FW3).
    body = _arm("welcome")
    assert "usage_view_set_plan(" not in body
    assert "plan_unconfirmed = true;" in body


def test_a_plan_confirms_and_a_usage_line_without_one_resets():
    plan = _arm("plan")
    assert "plan_unconfirmed = false;" in plan
    usage = _arm("usage")
    # The reset happens before anything of the line is drawn, so the
    # figures it carries are not dropped by it (usage_view_set_plan clears
    # a page whose kind changes).
    reset = re.search(r"if \(plan_unconfirmed\) \{\s*plan_unconfirmed = "
                      r"false;\s*usage_view_set_plan\(NULL, NULL, NULL, "
                      r"NULL, -1, -1, false,\s*false\);", usage)
    assert reset, "the usage arm does not reset an unconfirmed plan"
    assert reset.start() < usage.index("usage_view_set_provider1(p1)")
    assert reset.start() < usage.index("usage_view_update(sp,")
