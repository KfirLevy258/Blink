"""Run the theme builder page's own script in node, against the small DOM in
tests/support/builder_dom.js, and return what a scenario reports.

The page is taken as pc/theme_server.page() serves it, built-ins injected,
so a test drives the same script a browser gets. The markup is parsed here
with html.parser, since builder_dom.js has no parser of its own.
"""
import html.parser
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from pc import theme_server

DOM_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "builder_dom.js")
_VOID = {"meta", "input", "br", "img", "link", "hr", "path", "rect",
         "circle"}


class _Tree(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "body", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.script = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
            return
        node = {"tag": tag, "attrs": {k: v or "" for k, v in attrs},
                "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = {"tag": tag, "attrs": {k: v or "" for k, v in attrs},
                "children": []}
        self.stack[-1]["children"].append(node)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
            return
        if tag in _VOID:
            return
        while len(self.stack) > 1:
            if self.stack.pop()["tag"] == tag:
                break

    def handle_data(self, data):
        if self.in_script:
            self.script.append(data)
        elif data.strip():
            self.stack[-1]["children"].append(data)


def page_parts():
    page = theme_server.page()
    body = page[page.index("<body>") + len("<body>"):page.index("</body>")]
    t = _Tree()
    t.feed(body)
    return t.root, "".join(t.script)


def run(scenario, answers=None, tmp=None):
    """The scenario's return value. `answers` maps a path to the
    {"status": n, "body": {...}} the server gives it; anything not named
    answers 200 {"ok": true}."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    markup, script = page_parts()
    case = {"markup": markup, "script": script, "scenario": scenario,
            "answers": answers or {}}
    with tempfile.TemporaryDirectory(dir=tmp) as d:
        path = os.path.join(d, "case.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(case, f)
        out = subprocess.run([node, DOM_JS, path], capture_output=True,
                             text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)
