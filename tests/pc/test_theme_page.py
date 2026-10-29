"""The theme builder page's behaviour, run as a browser runs it.

The review of fp/builder-look found three behaviour bugs in its field
messages, each in a real browser, beside tests that checked the markup's
structure: a hex box's typing error outlived the value it was about, the
share box said "your board shows it now" over a code the board refused, and
every render rewrote the live regions, so a screen reader heard the same
warning over and over. These run the page's own script in node against
tests/support/builder_dom.js, and each was checked to fail on the code the
review read.
"""
import re

from pc import theme_server
from tests.support import builder_page

SAVED_DARK = {"/api/state": {"status": 200,
                             "body": {"saved": {"id": 0}, "draft": None}}}
# A faint theme: text 3.4:1 on its ground, readable enough to send and
# faint enough to earn a note under Text and Background.
FAINT = {"/api/state": {"status": 200, "body": {"saved": {
    "id": 12, "c": ["202020", "7a7a7a", "4387df", "0da243",
                    "0da243", "ba8107", "ff1900"],
    "w": 12, "ends": "round", "dot": 1, "by": "level"}, "draft": None}}}


def _code(c, text="111111", bg="121212"):
    import base64
    import json
    body = {"c": [bg, text] + c, "w": 12, "ends": "round", "dot": 1,
            "by": "level"}
    raw = json.dumps(body, separators=(",", ":")).encode()
    return "BT1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_a_hex_boxs_error_goes_when_a_preset_replaces_its_value():
    """S1: type #12345G, click away, pick Ocean. The box showed Ocean's
    colour under "Use six hex digits", still aria-invalid."""
    r = builder_page.run("""
    await advance(10);
    type('t-low', '#12345G');
    const before = [$('m-low').textContent, $('t-low').getAttribute('aria-invalid')];
    preset('Ocean');
    await advance(10);
    return { before, after: [$('m-low').textContent, $('t-low').getAttribute('aria-invalid'), $('t-low').value] };
    """, SAVED_DARK)
    assert r["before"] == ["Use six hex digits, 0 to 9 and A to F.", "true"]
    msg, invalid, value = r["after"]
    assert msg == "" and invalid == "false"
    assert re.fullmatch(r"#[0-9A-F]{6}", value)


def test_a_hex_boxs_error_goes_when_a_share_code_replaces_its_value():
    r = builder_page.run(f"""
    await advance(10);
    type('t-mid', '#ZZZZZZ');
    $('paste').focus(); $('paste').value = '{_code(["4387df", "0da243", "0da243", "ba8107", "ff1900"], text="e6e8eb", bg="0e1116")}';
    $('load').click();
    await advance(10);
    return [$('m-mid').textContent, $('t-mid').getAttribute('aria-invalid')];
    """, SAVED_DARK)
    assert r == ["", "false"]


def test_loaded_never_says_the_board_shows_a_code_it_refused():
    """S2: a valid code with unreadable text said "Loaded. The preview and
    your board show it now." while the bar said the board kept the last
    preview."""
    r = builder_page.run(f"""
    await advance(10);
    $('paste').focus(); $('paste').value = '{_code(["4387df", "0da243", "0da243", "ba8107", "ff1900"])}';
    $('load').click();
    await advance(300);
    return {{ field: $('m-paste').textContent, bar: $('status-text').textContent,
             previews: requests.filter(q => q.path === '/api/preview').length }};
    """, SAVED_DARK)
    assert r["previews"] == 0
    assert "unreadable" in r["field"] and "board keeps the last preview" in r["field"]
    assert "show it now" not in r["field"]


def test_loaded_goes_once_the_owner_moves_on():
    r = builder_page.run(f"""
    await advance(10);
    $('paste').focus(); $('paste').value = '{_code(["4387df", "0da243", "0da243", "ba8107", "ff1900"], text="e6e8eb", bg="0e1116")}';
    $('load').click();
    await advance(300);
    const loaded = $('m-paste').textContent;
    preset('Ocean');
    const afterPreset = $('m-paste').textContent;
    $('load').click();          // nothing pasted now: an error, then
    pick('c-high', '#ff0000');  // a colour picked by hand clears it too
    return [loaded, afterPreset, $('m-paste').textContent];
    """, SAVED_DARK)
    assert r == ["Loaded.", "", ""]


def test_a_live_region_is_written_only_when_its_words_change():
    """S5: ten ticks of the Session slider on a faint theme were twenty
    rewrites of the two live notes, each one announced again."""
    r = builder_page.run("""
    await advance(300);
    const at = liveWrites();
    const note = $('m-text').textContent;
    for (let i = 0; i < 10; i++) { $('p1').value = String(40 + i); $('p1').fire('input'); }
    const after = liveWrites();
    const grew = Object.keys(after).filter(k => after[k] !== at[k]);
    return { note, grew, bgLive: $('m-bg').getAttribute('aria-live') };
    """, FAINT)
    assert r["note"].startswith("Faint: text is")
    assert r["grew"] == []
    assert r["bgLive"] is None        # it repeats m-text word for word


# ------------------------------------------------------------- the CSS

def _rules(css):
    """(selector list, declarations) for each plain rule, @media bodies
    included, comments dropped."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for sel, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
        decls = dict(
            (k.strip(), v.strip()) for k, v in
            (d.split(":", 1) for d in body.split(";") if ":" in d))
        out.append(([s.strip() for s in sel.split(",")], decls))
    return out


def _px(v):
    m = re.fullmatch(r"(\d+)px", v or "")
    return int(m.group(1)) if m else None


def test_every_control_rule_is_at_least_44_px_tall():
    """S6: the segment buttons were 42 px, and the old check only found the
    string "min-height: 44px" somewhere in the file. This reads the rule
    each kind of control actually gets."""
    page = theme_server.page()
    css = page[page.index("<style>"):page.index("</style>")]
    rules = _rules(css)
    for control in (".btn", ".seg button", ".slot input[type=color]",
                    ".slot input[type=text]", ".paste input",
                    ".slider input", ".preset"):
        heights = [max(_px(d.get("min-height")) or 0, _px(d.get("height")) or 0)
                   for sels, d in rules if control in sels]
        assert heights and max(heights) >= 44, (control, heights)
