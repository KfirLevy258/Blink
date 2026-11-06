"""tools/page_shots.py against a loopback settings server and the mock.

Skipped without Playwright, which lives in .venv-test only. The pairs and the
diff ratios are written to the directory the tool prints (a tmp_path here;
run the tool by hand to keep them).
"""
import json
import os

import pytest

pytest.importorskip("playwright.sync_api")
pytest.importorskip("PIL")

from pc import theme_server  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK = os.path.join(ROOT, "docs", "design", "settings-page.html")


def _browsers_path():
    """conftest points HOME into tmp_path, and Playwright looks for its
    browsers under HOME, so name the real place."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return None
    try:
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    except ImportError:
        return None
    for sub in ("Library/Caches/ms-playwright", ".cache/ms-playwright"):
        if os.path.isdir(os.path.join(home, sub)):
            return os.path.join(home, sub)
    return None


@pytest.fixture
def shots(tmp_path, monkeypatch):
    where = _browsers_path()
    if where:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", where)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "page_shots", os.path.join(ROOT, "tools", "page_shots.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    s = theme_server.BuilderServer(str(tmp_path / ".blink"), tick_s=3600,
                                   idle_s=1000, persistent=True)
    s.start()
    out = tmp_path / "shots"
    try:
        assert mod.main(["--page-url", s.new_link(), "--mock", MOCK,
                         "--out", str(out)]) == 0
    finally:
        s.finish("test over")
        s.wait(5)
    return out


# Ceilings on the share of pixels that differ from the mock, with room for what
# legitimately differs: the mock's foot note (it centres the grid 21 px
# higher), example data, the system font, and the page's extra Customise
# content. Measured 2026-11-06: closed .055, Look .142, Connect .072,
# Board .076 (light); a layout that is off blows through them.
#
# Owner change 2026-11-07: the cards and the board start under the header
# (the mock centres them in the window), Save sits under the tabs, and the
# drawn screen has no dot. That moves more pixels away from the mock; the
# ceilings follow the new light-scheme measurements (closed .110, Look .232,
# Connect .144, Board .134) with about .015 of room, not further.
CEILING = {"closed": .125, "look": .25, "connect": .16, "board": .15}


def _box_close(a, b, tol=2):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_the_board_never_moves_and_the_layout_holds(shots):
    m = json.load(open(shots / "measurements.json"))
    mock = json.load(open(shots / "mock-measurements.json"))
    wide = {k: v for k, v in m.items() if k.endswith(("light", "dark"))}
    assert len(wide) == 8
    # The board's box is identical in all four states, both schemes.
    assert len({tuple(v["hero"]) for v in wide.values()}) == 1
    for k, v in wide.items():
        mk = mock[k]
        # The hero is the mock's size and column; its top may differ by the
        # foot note's shift, so the case is compared inside the hero.
        assert _box_close(v["hero"][::2][:1] + v["hero"][2:], mk["hero"][::2][:1] + mk["hero"][2:]), (k, v["hero"], mk["hero"])
        assert abs((v["caseTop"][1] - v["hero"][1]) -
                   (mk["caseTop"][1] - mk["hero"][1])) <= 2, k
        assert set(v["panels"]) == set(mk["panels"]), k
        for pid, (left, top, width, height) in v["panels"].items():
            # A panel's top is on the case's top (within the lip's 4 px), as
            # in the mock.
            assert abs(top - v["caseTop"][1]) <= 6, (k, pid)
            assert abs((top - v["caseTop"][1]) -
                       (mk["panels"][pid][1] - mk["caseTop"][1])) <= 6, (k, pid)
            cap = 372 if pid == "pConn" else 452
            assert width <= cap, (k, pid, width)
            assert abs(width - mk["panels"][pid][2]) <= 2, (k, pid)
            assert abs(left - mk["panels"][pid][0]) <= 2, (k, pid)
    narrow = m["look-400"]
    # No sideways scroll. The stable scrollbar gutter (the design's) holds
    # 15 px back on a classic-scrollbar desktop, so it is "at most", not "equal".
    assert narrow["scrollWidth"] <= narrow["viewport"]
    assert narrow["hub"] == "sticky"


def test_it_writes_eight_pairs_and_the_diffs_stay_under_their_ceilings(shots):
    diffs = json.load(open(shots / "diff.json"))
    assert len(diffs) == 8
    for key, ratio in diffs.items():
        assert ratio <= CEILING[key.split("-")[0]], (key, ratio)
        for who in ("page", "mock"):
            assert (shots / f"{key}-{who}.png").stat().st_size > 1000
