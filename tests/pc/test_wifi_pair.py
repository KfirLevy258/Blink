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
