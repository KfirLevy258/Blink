"""`blink theme` and its subcommands. ~ is tmp_path (tests/conftest.py)."""
import json
import os
import threading
import urllib.parse
import http.client

import pytest

from pc import cli
from pc import theme
from pc import theme_server
from tests.support.home import inside


def home():
    return cli.blink_home()


def test_reset_saves_dark(capsys):
    assert cli.main(["theme", "reset"]) == 0
    assert theme.load_saved(home()) == {"id": 0}
    assert "dark" in capsys.readouterr().out.lower()


def test_set_by_name(capsys):
    assert cli.main(["theme", "set", "ocean"]) == 0
    assert theme.load_saved(home()) == {"id": 4}


def test_set_kv_makes_a_custom_theme(capsys):
    assert cli.main(["theme", "set", "bg=#000000", "text=ffffff",
                     "width=thin", "dot=off"]) == 0
    t = theme.load_saved(home())
    assert t["id"] == 12 and t["c"][:2] == ["000000", "ffffff"]
    assert t["w"] == 5 and t["dot"] == 0


def test_set_a_share_code(capsys):
    src = {"id": 12, "c": ["000000", "ffffff", "111111", "222222",
                           "00ff00", "ffff00", "ff0000"],
           "w": 22, "ends": "flat", "dot": 0, "by": "gauge"}
    assert cli.main(["theme", "set", theme.to_share_code(src)]) == 0
    assert theme.load_saved(home()) == src


def test_set_refuses_unreadable_text_and_keeps_the_old_theme(capsys):
    cli.main(["theme", "set", "neon"])
    assert cli.main(["theme", "set", "bg=777777", "text=999999"]) == 2
    out = capsys.readouterr().out
    assert "3:1" in out
    assert theme.load_saved(home()) == {"id": 1}


def test_set_takes_the_number_list_shows(capsys):
    # `list` prints a number beside each theme; `set 4` used to answer
    # "no theme called '4'" (audit S1, 2026-10-28).
    assert cli.main(["theme", "set", "4"]) == 0
    assert theme.load_saved(home()) == {"id": 4}
    assert "Theme: Ocean" in capsys.readouterr().out


def test_set_takes_the_name_on_the_boards_tile(capsys):
    assert cli.main(["theme", "set", "Colour-blind"]) == 0
    assert theme.load_saved(home()) == {"id": 8}
    assert cli.main(["theme", "set", "retro", "lcd"]) == 2  # two words
    assert cli.main(["theme", "set", "Retro LCD"]) == 0
    assert theme.load_saved(home()) == {"id": 7}


def test_set_refuses_a_number_past_the_list_and_the_custom_number(capsys):
    cli.main(["theme", "set", "neon"])
    capsys.readouterr()
    assert cli.main(["theme", "set", "13"]) == 2
    assert "no theme called '13'" in capsys.readouterr().out
    # 12 is custom, which is made of colours rather than picked.
    assert cli.main(["theme", "set", "12"]) == 2
    assert "set with colours" in capsys.readouterr().out
    assert theme.load_saved(home()) == {"id": 1}


def test_a_damaged_share_code_is_called_damaged_not_missing_its_prefix(
        capsys):
    assert cli.main(["theme", "set", "BT1.garbage"]) == 2
    out = capsys.readouterr().out
    assert "damaged or cut short" in out
    assert "they start BT1." not in out
    good = theme.to_share_code({"id": 12, "c": ["000000", "ffffff",
                                                "111111", "222222", "00ff00",
                                                "ffff00", "ff0000"],
                                "w": 22, "ends": "flat", "dot": 0,
                                "by": "gauge"})
    assert cli.main(["theme", "set", good[:len(good) // 2]]) == 2
    assert "damaged or cut short" in capsys.readouterr().out
    assert theme.load_saved(home()) is None


def test_set_refuses_an_unknown_key(capsys):
    assert cli.main(["theme", "set", "sparkle=on"]) == 2
    assert theme.load_saved(home()) is None


def test_set_with_nothing_says_how(capsys):
    assert cli.main(["theme", "set"]) == 2


def test_show_prints_the_theme_and_its_share_code(capsys):
    cli.main(["theme", "set", "bg=000000", "text=ffffff"])
    capsys.readouterr()
    assert cli.main(["theme", "show"]) == 0
    out = capsys.readouterr().out
    code = theme.to_share_code(theme.load_saved(home()))
    assert code in out
    assert "blink theme set" in out and "bg=000000" in out


def test_show_a_built_in(capsys):
    cli.main(["theme", "set", "forest"])
    capsys.readouterr()
    cli.main(["theme", "show"])
    out = capsys.readouterr().out
    assert "forest" in out


def test_show_with_nothing_saved(capsys):
    assert cli.main(["theme", "show"]) == 0
    assert "board" in capsys.readouterr().out.lower()


def test_list_names_every_theme_and_marks_the_current_one(capsys):
    cli.main(["theme", "set", "mono"])
    capsys.readouterr()
    assert cli.main(["theme", "list"]) == 0
    out = capsys.readouterr().out
    for n in theme.NAMES:
        assert n.replace("_", "-") in out
    mark = [l for l in out.splitlines() if "*" in l]
    assert len(mark) == 1 and "mono" in mark[0]


def _board_json(**known):
    os.makedirs(home(), exist_ok=True)
    with open(os.path.join(home(), "board.json"), "w") as f:
        json.dump(dict({"port": "/dev/x", "board_id": "ab", "fw": "1.3.4"},
                       **known), f)


def test_an_old_board_is_named_when_a_theme_is_set(capsys):
    # Shipped 1.3.4 lists no caps, which board.json records as [].
    _board_json(caps=[])
    cli.main(["theme", "set", "neon"])
    out = capsys.readouterr().out
    assert "firmware 1.3.4, which does not take themes" in out


def test_a_board_with_the_cap_is_not_called_old_whatever_its_number(capsys):
    _board_json(caps=["svc", "theme"])
    cli.main(["theme", "set", "neon"])
    assert "does not take themes" not in capsys.readouterr().out


def test_a_board_json_from_before_caps_says_nothing(capsys):
    # Written by an older daemon: no caps key at all, so nothing is known.
    _board_json()
    cli.main(["theme", "set", "neon"])
    assert "does not take themes" not in capsys.readouterr().out


def test_bare_theme_serves_the_builder_and_opens_it(monkeypatch, capsys):
    opened = []

    def opener(url):
        opened.append(url)
        # The person saves in the page, from another thread, as a browser
        # would.
        def save():
            link = urllib.parse.urlsplit(url)
            c = http.client.HTTPConnection("127.0.0.1", link.port,
                                           timeout=5)
            c.request("GET", link.path + "?" + link.query)
            r = c.getresponse()
            cookie = r.getheader("Set-Cookie").split(";", 1)[0]
            r.read()
            c.request("POST", "/api/save", body=json.dumps({"id": 9}),
                      headers={"Cookie": cookie,
                               "Content-Type": "application/json"})
            c.getresponse().read()
            c.close()
        threading.Thread(target=save).start()
        return True

    monkeypatch.setattr(theme_server, "open_url", opener)
    assert cli.main(["theme"]) == 0
    assert opened and urllib.parse.urlsplit(opened[0]).hostname == \
        "127.0.0.1"
    assert theme.load_saved(home()) == {"id": 9}
    assert "saved" in capsys.readouterr().out.lower()


def test_bare_theme_with_no_browser_prints_the_link(monkeypatch, capsys):
    def opener(url):
        def cancel():
            # Somebody pastes the link elsewhere and cancels.
            link = urllib.parse.urlsplit(url)
            c = http.client.HTTPConnection("127.0.0.1", link.port,
                                           timeout=5)
            c.request("GET", link.path + "?" + link.query)
            r = c.getresponse()
            cookie = r.getheader("Set-Cookie").split(";", 1)[0]
            r.read()
            c.request("POST", "/api/cancel", body=b"",
                      headers={"Cookie": cookie})
            c.getresponse().read()
            c.close()
        threading.Timer(0.2, cancel).start()
        return False

    monkeypatch.setattr(theme_server, "open_url", opener)
    assert cli.main(["theme"]) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:" in out
    assert theme.take_preview(home()) == ("cancel", None)


def test_ctrl_c_in_bare_theme_cancels_without_an_unbounded_wait(
        monkeypatch, capsys):
    """Review of 1.4.0 (builder), S2: the command prints "so does Ctrl-C
    here" and then blocked in a wait with no timeout, which CPython 3.11
    cannot interrupt on Windows. The wait it makes must be timed. Here the
    unbounded one is made to fail, and a Ctrl-C (interrupt_main, which
    raises KeyboardInterrupt in the main thread the way the signal does) is
    sent once the builder is up."""
    import _thread
    real = theme_server.BuilderServer.wait

    def timed_only(self, timeout=None):
        assert timeout is not None and timeout <= 10, "an unbounded wait"
        return real(self, timeout)
    monkeypatch.setattr(theme_server.BuilderServer, "wait", timed_only)

    def opener(url):
        threading.Timer(0.3, _thread.interrupt_main).start()
        return True

    monkeypatch.setattr(theme_server, "open_url", opener)
    assert cli.main(["theme"]) == 0
    assert "Cancelled." in capsys.readouterr().out
    assert theme.take_preview(home()) == ("cancel", None)


def test_a_missing_page_is_one_sentence(monkeypatch, capsys):
    monkeypatch.setattr(theme_server, "PAGE_FILE", "not-there.html")
    monkeypatch.setattr(theme_server, "open_url", lambda u: True)
    assert cli.main(["theme"]) == 1
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert len([l for l in out.splitlines() if l.strip()]) == 1
    assert "builder" in out.lower()


def test_bare_theme_says_it_is_opening_and_gives_the_link(monkeypatch,
                                                         capsys):
    links = []

    def opener(url):
        links.append(url)
        def cancel():
            link = urllib.parse.urlsplit(url)
            c = http.client.HTTPConnection("127.0.0.1", link.port,
                                           timeout=5)
            c.request("GET", link.path + "?" + link.query)
            r = c.getresponse()
            cookie = r.getheader("Set-Cookie").split(";", 1)[0]
            r.read()
            c.request("POST", "/api/cancel", body=b"",
                      headers={"Cookie": cookie})
            c.getresponse().read()
            c.close()
        threading.Timer(0.2, cancel).start()
        return True

    monkeypatch.setattr(theme_server, "open_url", opener)
    cli.main(["theme"])
    out = capsys.readouterr().out
    assert "opening" in out.lower()
    assert links[0] in out


# ------------------------------------------------------------- set vs preview

def _sent_after_set(h, stored, args):
    """What a daemon's next tick sends after `blink theme set <args>`, on a
    board holding `stored` while a builder's preview is showing. `h` is the
    caller's home, the one `blink` resolves under pytest; checked before
    anything is written into it (tests/support/home.py)."""
    inside(h, theme.theme_path(h))
    inside(h, theme.preview_path(h))
    theme.save(h, stored)
    sync = theme.ThemeSync(h)
    sync.board_now({"t": "theme_now", "v": 2, "id": stored["id"],
                    "why": "boot"})
    theme.write_preview(h, {"id": 3})
    assert sync.outgoing() == [theme.message("preview", {"id": 3})]
    assert cli.main(["theme", "set"] + args) == 0
    return sync.outgoing()


def test_setting_the_stored_theme_ends_a_preview(capsys):
    # Review NICE-4: no save is owed, so only a cancel ends the preview.
    assert _sent_after_set(home(), {"id": 4}, ["ocean"]) == [
        theme.message("cancel")]


def test_setting_a_new_theme_sends_the_save_and_no_cancel(capsys):
    assert _sent_after_set(home(), {"id": 4}, ["neon"]) == [
        theme.message("save", {"id": 1})]


def test_show_says_when_the_boards_choice_was_kept(capsys):
    # Review of fp/fix-pc, N6: the board wins when both changed while the
    # app was off, and `show` is where the owner finds out why.
    h = home()
    theme.save(h, {"id": 4})
    theme.ThemeSync(h).board_now({"t": "theme_now", "v": 2, "id": 4,
                                  "why": "saved"}, board_id="ab12")
    theme.save(h, {"id": 7})             # set here while the app was off
    s = theme.ThemeSync(h)
    s.hello()
    s.board_now({"t": "theme_now", "v": 2, "id": 1, "why": "boot"},
                board_id="ab12")         # and Neon picked on the board
    capsys.readouterr()
    assert cli.main(["theme", "show"]) == 0
    out = capsys.readouterr().out
    assert "Theme: Neon" in out
    assert "kept over the theme set here then (Retro LCD)" in out
    assert "blink theme set retro-lcd" in out
    assert cli.main(["theme", "set", "ocean"]) == 0
    capsys.readouterr()
    cli.main(["theme", "show"])
    assert "kept over" not in capsys.readouterr().out


def test_set_refuses_to_write_over_an_unreadable_theme_json(capsys):
    h = home()
    os.makedirs(h, exist_ok=True)
    path = inside(h, theme.theme_path(h))
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"id": 4,')
    assert cli.main(["theme", "set", "ocean"]) == 1
    out = capsys.readouterr().out
    assert "could not be read" in out
    assert open(path, encoding="utf-8").read() == '{"id": 4,'


# ------------------------------------------------------------- uninstall

def test_uninstall_removes_every_theme_file(tmp_path, monkeypatch, capsys):
    # Review S7: kept, a reinstall sent the old theme to the board on its
    # first connect. Every name in theme.FILES, their temp siblings, and the
    # mkstemp names of the first development builds.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".claude").mkdir()
    h = home()
    theme.save(h, {"id": 4})
    theme.write_preview(h, {"id": 2})
    theme.ThemeSync(h).board_now({"t": "theme_now", "v": 2, "id": 4,
                                  "why": "saved"}, board_id="ab12")
    strays = [os.path.join(h, "theme_preview.json.taken"),
              theme.theme_path(h) + ".0123456789abcdef.blink-tmp",
              theme.sent_path(h) + ".fedcba9876543210.blink-tmp",
              os.path.join(h, ".theme-k2j3h4.tmp")]
    for p in strays:
        open(p, "w").close()
    for name in theme.FILES:
        assert os.path.exists(os.path.join(h, name)), name
    assert os.path.exists(theme.seen_path(h))
    keep = os.path.join(h, "signing-key-stand-in")
    open(keep, "w").close()
    cli.main(["uninstall"])
    left = sorted(os.listdir(h))
    assert not [n for n in left if "theme" in n], left
    assert os.path.exists(keep)


def test_every_theme_file_the_module_writes_is_listed():
    # FILES is what uninstall goes by; a new *_FILE must be in it.
    names = {v for k, v in vars(theme).items()
             if k.endswith("_FILE") and isinstance(v, str)}
    assert names and names <= set(theme.FILES)
