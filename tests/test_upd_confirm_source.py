"""The install confirm is withdrawn before a FAILED notice can land on it.

upd_confirm_withdraw() is host-tested as a truth table (tests/upd_prompt),
but what kept the confirm and the failure notice from ever being drawn
together -- the deepest stack the LVGL pool can reach, under 1.3 KB from dry
at the old pool size (full review #2, M1) -- is where upd_timer_cb calls it:
before the switch whose FAILED case raises the notice. A refactor that moved
the call below that case would still pass the truth table and the build. The
harness and board scripts catch it, but neither runs in pytest.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "firmware" / "src"


def _body(name):
    src = (SRC / "ui_settings.c").read_text(encoding="utf-8")
    m = re.search(r"\nstatic void " + name + r"\([^)]*\)\n\{(.*?)\n\}\n",
                  src, re.S)
    assert m, name
    return m.group(1)


def test_the_timer_withdraws_the_confirm_before_raising_a_failure():
    body = _body("upd_timer_cb")
    withdraw = body.index("upd_confirm_withdraw(")
    failed = body.index("case OTA_UI_FAILED:")
    assert withdraw < failed
    # And the withdrawal goes through confirm_close(), which reports the
    # confirm's [mem] peak and pays a rebuild owed while it was up.
    assert "confirm_close();" in body[withdraw:failed]


def test_yes_asks_the_same_question_before_it_installs():
    body = _body("install_yes_cb")
    ask = body.index("upd_confirm_withdraw(")
    assert ask < body.index("ota_request_install();")
    assert "confirm_ver" in body[ask:]


def test_every_way_the_confirm_goes_through_confirm_close():
    src = (SRC / "ui_settings.c").read_text(encoding="utf-8")
    # The only bare delete of the confirm is inside confirm_close itself;
    # do_close takes it with the whole panel.
    assert src.count("lv_obj_del(confirm);") == 1
    assert "lv_obj_del(confirm);" in _body("confirm_close")


def test_update_now_asks_the_same_question_before_it_installs():
    # The offer on the gauges took the consent without asking: a tap inside
    # the 250 ms tick after a welcome's CHECKING answered "The app stopped
    # answering", and a newer release offered within one tick installed
    # under the older text (fp/pool review r2, NICE-1; full review #3, N2).
    body = _body("upd_prompt_now_cb")
    ask = body.index("upd_confirm_withdraw(")
    assert ask < body.index("ota_request_install();")
    assert "prompt_ver" in body[ask:]
    # A changed offer is refused, not installed: the refusal returns
    # before the install line.
    refused = body[ask:body.index("ota_request_install();")]
    assert "install not started" in refused and "return;" in refused


def test_the_prompt_records_the_version_it_draws():
    # Recorded in the drawing, not in upd_prompt_show, so a theme repaint
    # that redraws the box from the live state keeps the two in step.
    body = _body("upd_prompt_draw")
    assert "prompt_ver" in body[:body.index("lv_obj_create(")]


def test_a_refused_update_now_lets_the_offer_ask_again():
    # The owner said yes and got nothing; without lifting the once-a-boot
    # latch, a newer release that replaced the one shown would go
    # unannounced on the gauges until the next boot.
    body = _body("upd_prompt_now_cb")
    refused = body[body.index("upd_confirm_withdraw("):
                   body.index("ota_request_install();")]
    assert "upd_prompt_done = false;" in refused
