"""BLINK_TAP never carries a WiFi credential, in any of its three streams.

The transcript is attached to bug reports. With WiFi on the wire a message can
hold the network's name and password, the pairing key and the board's address,
and the console path used to record every raw line. These tests push each
shape of that through a real Tap and search the file for what must not be there.
"""
import json

import claude_usage_bridge as cub

SSID_HEX = "6e6574776f726b2d53454352455421"
PASS_HEX = "70617373776f72642d534543524554"
KEY_HEX = "ab" * 32
ADDR = "192.168.1.23"
UNQUOTED = "[cfg] mode=1 ssid=home-net-SECRET token=present"
STATE = ('{"t":"wifi_state","st":"up","link":"wifi","s":"%s","ip":"%s",'
         '"port":49731,"r":-50,"kid":"0a1b2c3d"}' % (SSID_HEX, ADDR))
SECRETS = (SSID_HEX, PASS_HEX, KEY_HEX, "home-net-SECRET", ADDR, "49731")


def _records(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_tx_rx_and_console_never_carry_wifi_fields(tmp_path):
    path = tmp_path / "tap.jsonl"
    tap = cub.Tap(str(path))
    tap.tx({"t": "wifi_set", "v": 2, "s": SSID_HEX, "p": PASS_HEX,
            "k": KEY_HEX}, sent=True)
    tap.rx(json.loads(STATE))
    tap.console((STATE + "\r\n").encode())
    half = len(STATE) // 2
    tap.console(STATE[:half].encode())                  # split across calls
    tap.console((STATE[half:] + "\n").encode())
    tap.console((STATE[STATE.index("{") + 1:] + "\n").encode())  # no brace
    tap.console(("[usage] sess" + STATE + "\n").encode())
    tap.console((UNQUOTED + "\n").encode())
    text = path.read_text()
    for secret in SECRETS:
        assert secret not in text, secret
    recs = _records(path)
    assert recs[0]["msg"] == {"t": "wifi_set"} and recs[0]["sent"] is True
    assert recs[1]["msg"]["st"] == "up" and recs[1]["msg"]["kid"] == "0a1b2c3d"


def test_console_keeps_only_allowlisted_forms(tmp_path):
    path = tmp_path / "tap.jsonl"
    tap = cub.Tap(str(path))
    tap.console(b"[usage] session 50% (12s)  weekly 4% (9s)  STALE\n"
                b'{"t":"ping","k":"SECRET"}\n'
                b"[proto] ota failed: SECRET\n"
                b"rst:0xc (SW_CPU_RESET),boot:0x13\n"
                b"noise \xff SECRET\n")
    assert [r["line"] for r in _records(path)] == [
        "[usage] session applied STALE", '{"t":"ping"}',
        "[proto] ota failed", "rst:0xc (SW_CPU_RESET)", "(board line)"]
    assert "SECRET" not in path.read_text()
