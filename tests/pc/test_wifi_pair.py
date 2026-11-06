from pc import wifi_pair


def test_password_rules():
    assert wifi_pair.password_problem("1234567")
    assert wifi_pair.password_problem("12345678") is None
    assert wifi_pair.password_problem('a"b\\c' + "d" * 58) is None
    assert wifi_pair.password_problem("x" * 64)
    assert wifi_pair.password_problem("ab" * 32) is None
    assert "ASCII" in wifi_pair.password_problem("p\u00e4ssw\u00f6rd")
    assert wifi_pair.password_problem("a" * 65)


def test_ssid_rules():
    assert wifi_pair.ssid_problem(b"")
    assert wifi_pair.ssid_problem(b"a\x00b")
    assert wifi_pair.ssid_problem(b"x" * 33)
    assert wifi_pair.ssid_problem(b"\xf0\x9f\x8d\x95 pizza") is None


# ------------------------------------------------- the pairing file (7.3)

import json
import os

import pytest

from pc import cli, private_file

KEY = bytes(range(32))


def _blink(tmp_path):
    return str(tmp_path / ".blink")


def test_save_round_trip(tmp_path):
    home = _blink(tmp_path)
    assert wifi_pair.load(home) is None
    key = wifi_pair.new_key()
    assert len(key) == 32 and key != wifi_pair.new_key()
    wifi_pair.save(home, "20500d342b68", key, "192.168.1.23", 49731)
    assert wifi_pair.pair_path(home) == os.path.join(home, "private",
                                                     "wifi_pair.json")
    doc = json.loads(open(wifi_pair.pair_path(home), "rb").read())
    assert doc == {"v": 1, "board_id": "20500d342b68", "key": key.hex(),
                   "kid": wifi_pair.kid(key), "ip": "192.168.1.23",
                   "port": 49731}
    assert wifi_pair.kid(KEY) == "630dcd29"     # sha256(00..1f)[:4]
    got = wifi_pair.load(home)
    assert got["key"] == key and got["board_id"] == "20500d342b68"
    wifi_pair.update_ip(home, "10.0.0.7", 49732)
    got = wifi_pair.load(home)
    assert (got["ip"], got["port"], got["key"]) == ("10.0.0.7", 49732, key)
    assert wifi_pair.remove(home) is True
    assert wifi_pair.load(home) is None
    assert wifi_pair.remove(home) is False


def _plant(home, doc):
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    private_file.write_private(home, wifi_pair.PAIR_FILE, raw)


def test_load_refuses_bad_shapes(tmp_path):
    home = _blink(tmp_path)
    good = {"v": 1, "board_id": "20500d342b68", "key": KEY.hex(),
            "kid": wifi_pair.kid(KEY), "ip": "192.168.1.23", "port": 49731}
    _plant(home, good)
    assert wifi_pair.load(home) is not None
    bad = [b"not json", b"[]", b"\xff",
           dict(good, v=2), dict(good, key=KEY.hex()[:-2]),
           dict(good, key="zz" * 32), dict(good, kid="00000000"),
           dict(good, port=0), dict(good, port=True), dict(good, port="1"),
           dict(good, ip=7), dict(good, board_id=None),
           {k: v for k, v in good.items() if k != "ip"}]
    for doc in bad:
        _plant(home, doc)
        assert wifi_pair.load(home) is None, doc


def test_load_logs_once_on_not_private(tmp_path, capsys, monkeypatch):
    home = _blink(tmp_path)
    monkeypatch.setattr(wifi_pair, "_said_not_private", False)

    def refuse(*a, **kw):
        raise private_file.NotPrivate("wrong owner")

    monkeypatch.setattr(wifi_pair.private_file, "open_private", refuse)
    assert wifi_pair.load(home) is None
    assert wifi_pair.load(home) is None
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "owner-only" in err
    assert "wrong owner" not in err      # the fixed sentence, nothing more


def test_uninstall_removes_the_pairing(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    cli.main(["install"])
    home = cli.blink_home()
    wifi_pair.save(home, "20500d342b68", KEY, "192.168.1.23", 49731)
    cli.main(["uninstall"])
    assert not os.path.exists(wifi_pair.pair_path(home))
    assert not os.path.exists(os.path.join(home, "private"))
