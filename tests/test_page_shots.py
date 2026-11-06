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


def test_the_board_never_moves_and_the_layout_holds(shots):
    m = json.load(open(shots / "measurements.json"))
    wide = {k: v for k, v in m.items() if k.endswith(("light", "dark"))}
    assert len(wide) == 8
    # The board's box is identical in all four states, both schemes.
    assert len({tuple(v["hero"]) for v in wide.values()}) == 1
    for k, v in wide.items():
        for pid, (left, top, width, height) in v["panels"].items():
            # A panel's top is on the case's top (within the lip's 4 px).
            assert abs(top - v["caseTop"][1]) <= 6, (k, pid)
            cap = 372 if pid == "pConn" else 452
            assert width <= cap, (k, pid, width)
    narrow = m["look-400"]
    # No sideways scroll. The stable scrollbar gutter (the design's) holds
    # 15 px back on a classic-scrollbar desktop, so it is "at most", not "equal".
    assert narrow["scrollWidth"] <= narrow["viewport"]
    assert narrow["hub"] == "sticky"


def test_it_writes_eight_pairs_and_their_diff_ratios(shots):
    diffs = json.load(open(shots / "diff.json"))
    assert len(diffs) == 8 and all(0 <= r <= 1 for r in diffs.values())
    for key in diffs:
        for who in ("page", "mock"):
            assert (shots / f"{key}-{who}.png").stat().st_size > 1000
