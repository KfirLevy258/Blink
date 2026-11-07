import json
import os

from pc import link_status as ls

NOW = 1000.0


def _rec(tmp_path, **kw):
    ls.write(str(tmp_path), kw.get("link", "usb"), kw.get("ip"),
             now=lambda: kw.get("t", NOW))
    return ls.read(str(tmp_path), now=lambda: NOW)


def test_status_link_usb(tmp_path):
    assert ls.status_line(_rec(tmp_path), False) == "Link        USB cable"


def test_status_link_wifi_with_address(tmp_path):
    rec = _rec(tmp_path, link="wifi", ip="192.168.1.23")
    assert ls.status_line(rec, True) == "Link        WiFi, board at 192.168.1.23"


def test_status_link_wifi_not_found(tmp_path):
    rec = _rec(tmp_path, link="wifi")
    assert ls.status_line(rec, True) == "Link        WiFi set up, board not found"


def test_status_link_stale_record_says_not_reporting(tmp_path):
    assert _rec(tmp_path, t=NOW - 181) is None
    assert ls.status_line(None, True) == "Link        unknown (app not reporting)"


def test_status_link_junk_address_is_not_printed(tmp_path):
    rec = {"link": "wifi", "ip": "evil\x1b[2J"}
    assert "evil" not in ls.status_line(rec, True)


def test_write_is_atomic_and_home_resolved_in_function(tmp_path):
    ls.write(str(tmp_path), "usb", None, now=lambda: NOW)
    assert sorted(os.listdir(tmp_path)) == ["link.json"]
    assert json.load(open(tmp_path / "link.json")) == {
        "link": "usb", "ip": None, "t": NOW}
    assert ls.read(str(tmp_path / "missing")) is None
    (tmp_path / "link.json").write_text("{")
    assert ls.read(str(tmp_path)) is None
