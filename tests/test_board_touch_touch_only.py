"""tools/board_touch.py --touch-only, `expect ui_state k=a|b` and
--skip-missing, and the two WiFi bench scripts that rely on them.

--touch-only exists for the WiFi bench: a board holding a live session must
hear touch, ui_state and widgets lines from the bench and nothing else. What
is pinned here is that the tool cannot say anything else, even by accident.
No board and no port; the fake board is the one from test_board_touch_widgets.
"""
import io
import json
from pathlib import Path

import pytest

from tests.test_board_touch_widgets import WidgetBoard, _w, bt

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [ROOT / "tools" / "bench" / "session_touch.txt",
           ROOT / "tools" / "bench" / "download_touch.txt"]


def _bench(rows=None, **kw):
    holder = {}
    link = WidgetBoard(lambda: holder["b"], rows or [_w(0, "Update")])
    b = bt.Bench(link, out=io.StringIO(), settle_scale=0, **kw)
    holder["b"] = b
    return b, link


def _types(link):
    return [json.loads(w)["t"] for w in link.writes]


def test_touch_only_sends_only_injector_types():
    b, link = _bench(touch_only=True)
    b.greet()
    b.send_frames()
    b.run_script(bt.parse_script(
        "expect ui_state scr=gauges\n"
        "raw tap 10 10\n"
        "widgets\n"
        "mem\n"
        'click "Update"\n'))
    assert not b.failures, b.failures
    assert link.writes
    assert set(_types(link)) <= bt.INJECT_TYPES
    for banned in ("welcome", "time", "usage", "svc", "pong"):
        assert banned not in _types(link)


def test_touch_only_does_not_answer_the_boards_own_lines():
    b, link = _bench(touch_only=True)
    for line in (b'{"t":"ping","v":2}', b'{"t":"ota_query","v":2}',
                 b'{"t":"theme_edit","v":2}'):
        b.on_line(line)
    assert link.writes == []


def test_touch_only_refuses_fake_steps(tmp_path, monkeypatch, capsys):
    for text in ("fake ota_none\n", "edit-reply ok\n"):
        script = tmp_path / "s.txt"
        script.write_text(text)

        def no_port(args):
            raise AssertionError("the port was opened")
        monkeypatch.setattr(bt, "_connect", no_port)
        assert bt.main(["--touch-only", "script", str(script)]) == 2
        assert bt.TOUCH_ONLY_REFUSAL in capsys.readouterr().err


def test_touch_only_refuses_non_injector_types():
    b, link = _bench(touch_only=True)
    with pytest.raises(ValueError):
        b.send({"t": "welcome", "v": 2})
    assert link.writes == []
    assert b.send({"t": "ui_state", "v": 2})
    assert b.send({"t": "widgets", "v": 2, "page": 0})


def test_expect_alternation():
    b, _ = _bench()
    b.run_script(bt.parse_script("expect ui_state scr=settings|gauges\n"
                                 "expect ui_state scr=gauges|eyes\n"))
    assert not b.failures
    b.run_script(bt.parse_script("expect ui_state scr=settings|eyes\n"))
    assert len(b.failures) == 1


def test_expect_ui_state_accepts_the_connection_overlay():
    # Found on the bench during the RAM spike: session_touch.txt expects it.
    bt.parse_script("expect ui_state ov=connection\n")
    obj = {k: (w[0] if k in bt._STATE_WORDS else bt._STATE_INTS[k][0])
           for k, w in {**bt._STATE_WORDS, **bt._STATE_INTS}.items()
           if k in bt.STATE_KEYS}
    assert bt.parse_ui_state({**obj, "ov": "connection"}) is not None


def test_skip_missing_skips_an_absent_widget_and_nothing_else():
    script = bt.parse_script('click "Forget WiFi"\n'
                             'expect-widget "Link: WiFi"\n'
                             'click "Update"\n')
    b, link = _bench(skip_missing=True)
    b.run_script(script)
    assert not b.failures, b.failures
    out = b.out.getvalue()
    assert out.count("skip ") == 2 and "--skip-missing" in out
    assert [c["op"] for c in [json.loads(w) for w in link.writes]
            if c["t"] == "touch"] == ["click"]

    b, _ = _bench()
    b.run_script(script)
    assert b.failures


def test_skip_missing_skips_a_tap_whose_screen_is_not_showing():
    # settings.back needs scr=settings; the fake board is on the gauges.
    b, link = _bench(skip_missing=True)
    b.run_script(bt.parse_script("tap settings.back\n"))
    assert not b.failures, b.failures
    assert "skip tap settings.back" in b.out.getvalue()
    assert "touch" not in _types(link)

    b, _ = _bench()
    b.run_script(bt.parse_script("tap settings.back\n"))
    assert b.failures


# The labels the bench scripts may name. Today's Settings screen, from
# firmware/src/ui_settings.c (mk_tile and the back button), plus the Connection
# tile the spike adds (Task 0.2) and Settings > Connection proper (Task 1.5).
# A literal list on purpose: a script that names a widget nobody has is a
# script that skips or fails on the bench without saying why.
WIDGETS = {"Brightness", "Theme", "Focus", "Update", "<",
           "Connection", "Forget WiFi", "Link: WiFi", "Cancel"}


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_bench_scripts_have_no_injection(path):
    steps = bt.parse_script(path.read_text(encoding="utf-8"))
    assert steps
    for s in steps:
        assert s.cmd not in ("fake", "edit_reply"), (path.name, s.line)
        if s.cmd in ("click", "expect_widget"):
            assert s.args[0] in WIDGETS, (path.name, s.line, s.args[0])
        if s.cmd == "tap":
            assert s.args[0] in bt.TARGETS, (path.name, s.line)


def test_skip_missing_does_not_skip_a_tap_under_a_notice():
    # settings.back needs scr=settings. With a notice up on the settings
    # screen the tap must dismiss it and go ahead, as it does without the
    # flag; the raw state has modal=notice, which is not "another screen".
    b, link = _bench(skip_missing=True)
    link.state.update(scr="settings", modal="notice", nt="ota")
    orig = link.write

    def write(raw):
        if json.loads(raw).get("op") == "dismiss":
            link.state.update(modal="none")
        orig(raw)
    link.write = write
    b.run_script(bt.parse_script("tap settings.back\n"))
    assert "skip tap" not in b.out.getvalue()
    assert "notice dismissed" in b.out.getvalue()


def test_touch_only_install_press_does_not_raise_at_the_end():
    # end_install's ota_error / ota_none are not injector types; send()
    # raised ValueError at the end of the run.
    b, link = _bench(rows=[_w(0, "Update now")], touch_only=True)
    b.install_pressed = True
    b.run_with_restore([], restore=False)
    assert set(_types(link)) <= bt.INJECT_TYPES
    assert b.install_pressed is False
    assert "left for the daemon" in b.out.getvalue()
