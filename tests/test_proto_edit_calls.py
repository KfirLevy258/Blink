"""proto.c hands "Edit on computer" every line and event that moves its hold.

Nothing on the host compiles proto.c -- it is Zephyr all the way down, and
the touch harness re-implements its dispatch (tools/panel_render/touch/
harness.c) -- so a review of fp/edit-held deleted each of its calls into
ui_settings.c one at a time, and every host check still passed: the
welcome no longer passed on, HOST LOST no longer passed on, and a preview
answering as an ok 1, which would hold the slot for a builder `blink theme`
opened and whose end the board is never told (SHOULD-3). The real proof is
a board, flashed and run; this pins the wiring, by reading the source, so
the cheap check fails first.

Each call is looked for inside the branch that must make it -- the
`strcmp(type, "...")` arm of dispatch(), the bye arm, the host-went-away
block of proto_service -- not just anywhere in the file.
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


def _calls(body):
    return set(re.findall(r"\b(ui_settings_theme_edit_\w+)\(", body))


def test_a_welcome_is_passed_on_whole():
    assert "ui_settings_theme_edit_welcome(json);" in _arm("welcome")


def test_a_preview_or_save_is_a_builder_seen_never_an_ok():
    body = _arm("theme")
    assert _calls(body) == {"ui_settings_theme_edit_builder_seen"}
    # ...and not for a cancel, which is a builder ending, not at work.
    assert re.search(r"if \(m\.op != THEME_OP_CANCEL\) \{\s*"
                     r"ui_settings_theme_edit_builder_seen\(\);", body)


def test_theme_edit_carries_done_before_ok():
    body = _arm("theme_edit")
    assert _calls(body) == {"ui_settings_theme_edit_done",
                            "ui_settings_theme_edit_reply"}
    assert body.index("ui_settings_theme_edit_done") < \
        body.index("ui_settings_theme_edit_reply")


def test_edit_state_is_passed_on_with_absent_as_shut():
    body = _arm("edit_state")
    assert _calls(body) == {"ui_settings_theme_edit_state"}
    assert re.search(r"double editing = 0;", body)
    assert "ui_settings_theme_edit_state(editing == 1);" in body


def test_bye_frees_the_hold():
    m = re.search(r'strcmp\(type, "bye"\) == 0\) \{(.*?)\n\t\}', SRC, re.S)
    assert m
    assert "ui_settings_theme_edit_host_gone();" in m.group(1)


def test_host_lost_frees_the_hold():
    m = re.search(r"if \(host_seen && \(now - last_host_ms\) > "
                  r"HOST_TIMEOUT_MS\) \{(.*?)\n\t\}", SRC, re.S)
    assert m, "the host-went-away block of proto_service has moved"
    assert "ui_settings_theme_edit_host_gone();" in m.group(1)


def test_no_other_call_into_the_hold():
    # A new caller is a new way in or out, and belongs in edit_hold.h's
    # list and in a test here before it ships.
    assert _calls(SRC) == {
        "ui_settings_theme_edit_welcome", "ui_settings_theme_edit_builder_seen",
        "ui_settings_theme_edit_done", "ui_settings_theme_edit_reply",
        "ui_settings_theme_edit_state", "ui_settings_theme_edit_host_gone"}
