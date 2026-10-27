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
