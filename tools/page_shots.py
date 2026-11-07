#!/usr/bin/env python3
"""Screenshot the settings page beside the owner-approved mock, and measure.

A dev tool, not part of the product. Playwright is not a dependency of BLINK
and lives in .venv-test only:

    PYTHONDONTWRITEBYTECODE=1 .venv-test/bin/python -m pip install playwright
    .venv-test/bin/python -m playwright install chromium

Pillow (also .venv-test only) draws the diff. Both are imported inside main(),
so importing this file does nothing.

    page_shots.py --page-url 'http://127.0.0.1:PORT/open?k=...' \\
                  --mock docs/design/settings-page.html --out DIR

It takes the opening state and each control (Look, Connect, Board) at 1440x900
in both colour schemes, the page and the mock side by side: eight pairs. Every
request that is not loopback or file: is aborted, so a page that reached for
the network shows it as a failure here, and the mock's Google Fonts fall back
to the system stack. It writes the PNGs, measurements.json (the board's box in
each state, each panel's top and width, the 400px width and the hub's
position) and mock-measurements.json (the same for the mock) and diff.json (per pair, the share of pixels that differ by more
than a few levels), prints the directory, and returns 0.

The page-url is a one-time link, spent once: the colour scheme is switched in
the same session, not by a new page.
"""
import argparse
import json
import os
import urllib.parse
import sys

STATES = ("closed", "Look", "Connect", "Board")
SCHEMES = ("light", "dark")
_MEASURE = """() => {
  const box = (s) => { const e = document.querySelector(s); if (!e) return null;
    const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const panels = {};
  document.querySelectorAll('.panel').forEach((p) => {
    if (p.hidden || p.offsetParent === null) return;
    const r = p.getBoundingClientRect(); panels[p.id] = [r.left, r.top, r.width, r.height]; });
  return { hero: box('#hero'), caseTop: box('#case'), panels,
           scrollWidth: document.scrollingElement.scrollWidth, viewport: document.documentElement.clientWidth,
           hub: getComputedStyle(document.querySelector('.hub')).position };
}"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--page-url", required=True)
    ap.add_argument("--mock", required=True, help="the design mock's .html")
    ap.add_argument("--out", required=True, help="a directory for the PNGs")
    a = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright
    from PIL import Image, ImageChops
    os.makedirs(a.out, exist_ok=True)
    measures, mock_measures, diffs = {}, {}, {}
    mock_url = "file://" + os.path.abspath(a.mock)

    def only_local(route):
        u = route.request.url
        # The host, not a prefix: http://127.0.0.1.example.com is not loopback.
        if u.startswith(("file:", "data:")) or \
                urllib.parse.urlsplit(u).hostname in ("127.0.0.1", "localhost"):
            route.continue_()
        else:
            route.abort()

    def press(page, state):
        if state != "closed":
            page.get_by_role("button", name=state, exact=True).first.click()
            page.wait_for_timeout(450)       # the panel's 320 ms entrance

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            ctx.route("**/*", only_local)
            page = ctx.new_page()
            page.goto(a.page_url)
            page.wait_for_selector("main:not([aria-busy])")
            mock = ctx.new_page()
            mock.goto(mock_url)
            mock.wait_for_timeout(300)
            for scheme in SCHEMES:
                page.emulate_media(color_scheme=scheme)
                mock.emulate_media(color_scheme=scheme)
                for state in STATES:
                    key = f"{state.lower()}-{scheme}"
                    press(page, state)
                    press(mock, state)
                    for pg, who in ((page, "page"), (mock, "mock")):
                        pg.screenshot(path=os.path.join(a.out, f"{key}-{who}.png"))
                    measures[key] = page.evaluate(_MEASURE)
                    mock_measures[key] = mock.evaluate(_MEASURE)
                    one = Image.open(os.path.join(a.out, key + "-page.png")).convert("RGB")
                    two = Image.open(os.path.join(a.out, key + "-mock.png")).convert("RGB")
                    d = ImageChops.difference(one, two).convert("L").point(
                        lambda v: 255 if v > 24 else 0)
                    diffs[key] = round(sum(1 for v in d.getdata() if v) /
                                       (d.width * d.height), 4)
                    press(page, state)          # pressing the open control closes it
                    press(mock, state)
            # The narrow window, Look open: nothing scrolls sideways, the hub sticks.
            page.emulate_media(color_scheme="light")
            page.set_viewport_size({"width": 400, "height": 800})
            press(page, "Look")
            page.screenshot(path=os.path.join(a.out, "look-400-page.png"))
            measures["look-400"] = page.evaluate(_MEASURE)
        finally:
            browser.close()
    with open(os.path.join(a.out, "measurements.json"), "w") as f:
        json.dump(measures, f, indent=1)
    with open(os.path.join(a.out, "mock-measurements.json"), "w") as f:
        json.dump(mock_measures, f, indent=1)
    with open(os.path.join(a.out, "diff.json"), "w") as f:
        json.dump(diffs, f, indent=1)
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
