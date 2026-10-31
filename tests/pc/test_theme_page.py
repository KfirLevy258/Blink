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

import pytest

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


# --------------------------------------------------- the inactive-tab notice
#
# A tab that stopped being the live builder said so only in the status bar,
# over controls that looked live, and most browsers refuse the
# window.close() meant to take it away (the owner's report, 2026-10-29).
# It shows a notice over an inert page now. Each test here reads the notice
# itself, so a page without it fails every one.

NOTICE = """
    const hid = (id) => $(id).getAttribute('hidden') !== null;
    const d = $('notice-dialog');
    return {
      shown: !hid('notice'),
      title: $('notice-title').textContent, why: $('notice-why').textContent,
      end: $('notice-end').textContent,
      role: d.getAttribute('role'), modal: d.getAttribute('aria-modal'),
      labelled: d.getAttribute('aria-labelledby'),
      focused: document.activeElement === d,
      inert: ['.top', '.studio'].map(s => document.querySelector(s).getAttribute('inert') !== null),
      offer: !hid('notice-copy') && !hid('notice-keep-text'),
      keep: $('notice-keep-text').textContent,
      described: d.getAttribute('aria-describedby'),
      status: $('status-text').textContent,
      live: document.querySelectorAll('header button, main button, main input')
              .filter(e => !e.disabled).map(e => e.id || e.tagName),
      closed: window.closed,
    };
"""

_OCEAN = "preset('Ocean'); await advance(300);"
_NEWER = """answer('/api/ping', () => ({ status: 200, body: { ok: true, newer: true } }));
    await advance(20000);"""
# A ping missed, and its retry 3 s later missed too.
_NO_ANSWER = "answer('/api/ping', () => 'fail'); await advance(23100);"

# What each ending's notice says. The heading was fixed in the markup as
# "This tab is no longer active." for every ending, so a save that worked
# (daemon log "[theme] saved", the board on the new theme) put up a
# full-page notice the owner read as an error (manual test on a board,
# 2026-10-30). The heading names the ending now, and the sentence under it
# says what follows from it rather than the heading again.
SAVED = ("Saved.", "Your board keeps the theme you saved.")
CANCELLED = ("Cancelled.", "Your board is back on its saved theme.")
SUPERSEDED = ("This tab is no longer active.", "The builder continued in a newer tab.")
STOPPED = ("The builder stopped.", "Run blink theme on your computer to start it again.")

# reason: (what the tab does first, what ends it, (heading, sentence))
REASONS = {
    "saved": ("", "$('save').click(); await advance(10);", SAVED),
    "cancelled": ("", "$('cancel').click(); await advance(10);", CANCELLED),
    "superseded": ("", _NEWER, SUPERSEDED),
    "stopped, no answer": ("", _NO_ANSWER, STOPPED),
    "stopped, refused": (
        "", "answer('/api/ping', () => ({ status: 403, body: {} })); await advance(23100);",
        STOPPED),
    # A failed preview asks a ping, and the ping and its retry fail too.
    "stopped, preview unanswered": (
        "answer('/api/preview', () => 'fail'); answer('/api/ping', () => 'fail');",
        "preset('Ocean'); await advance(3500);", STOPPED),
    # Save refused as superseded is the newer tab's ending, not a save.
    "save superseded": (
        "", "answer('/api/save', () => ({ status: 409, body: {} })); $('save').click(); await advance(10);",
        SUPERSEDED),
    # Save unanswered is a stop, whatever the owner pressed.
    "save unanswered": (
        "", "answer('/api/save', () => 'fail'); $('save').click(); await advance(10);",
        STOPPED),
    "cancel superseded": (
        "", "answer('/api/cancel', () => ({ status: 409, body: {} })); $('cancel').click(); await advance(10);",
        SUPERSEDED),
    # Cancel unanswered is a stop too: the builder that was to be cancelled
    # is not there, and "Cancelled." would say the board was put back when
    # nothing was heard to have done it (review of fp/notice-title, SHOULD-1).
    "cancel unanswered": (
        "", "answer('/api/cancel', () => 'fail'); $('cancel').click(); await advance(10);",
        STOPPED),
    # Save answered with something it does not expect (a proxy's 502, a
    # 403 from a builder that is no longer this session's) is a stop as well,
    # not a superseded tab and not a tab left live under "Saving...".
    "save refused, not 400/409/500": (
        "", "answer('/api/save', () => ({ status: 403, body: {} })); $('save').click(); await advance(10);",
        STOPPED),
}


@pytest.mark.parametrize("reason", REASONS)
def test_every_ending_shows_the_notice_with_its_reason(reason):
    before, ending, (title, sentence) = REASONS[reason]
    r = builder_page.run(f"""
    await advance(10);
    {before}
    {ending}
    """ + NOTICE, SAVED_DARK)
    assert r["shown"] is True
    assert r["title"] == title
    assert r["why"] == sentence
    assert r["title"] != r["why"]
    if (title, sentence) in (STOPPED, CANCELLED):
        # The bar is the record under the notice and begins with the same
        # words, tense included: it said "has stopped" under "stopped".
        assert r["status"].startswith(f"{title} {sentence}")
    assert r["end"] == "You can close this tab."
    assert (r["role"], r["modal"]) == ("dialog", "true")
    assert r["labelled"] == "notice-title"
    assert r["focused"] is True
    assert r["inert"] == [True, True]
    assert r["live"] == []          # every control behind it, copy included


def test_a_tab_whose_builder_never_answered_shows_the_notice():
    r = builder_page.run("await advance(10);" + NOTICE,
                         {"/api/state": {"status": 500, "body": {}}})
    assert r["shown"] is True and (r["title"], r["why"]) == STOPPED
    assert r["offer"] is False      # it never had the owner's theme


def test_a_live_tab_shows_no_notice():
    r = builder_page.run("await advance(10);" + _OCEAN + NOTICE, SAVED_DARK)
    assert r["shown"] is False
    assert r["inert"] == [False, False]
    assert "save" in r["live"] and "copy-code" in r["live"]


@pytest.mark.parametrize("before, ending, offer", [
    (_OCEAN, _NEWER, True),
    ("", _NEWER, False),
    (_OCEAN, _NO_ANSWER, True),
    ("", _NO_ANSWER, False),
    (_OCEAN, "$('save').click(); await advance(10);", False),
    (_OCEAN, "$('cancel').click(); await advance(10);", False),
], ids=["newer, draft", "newer, none", "stopped, draft", "stopped, none",
        "saved", "cancelled"])
def test_the_copy_button_is_offered_only_with_a_draft(before, ending, offer):
    """Cancel throws the changes away on purpose and Save keeps them, so
    only a tab taken over or stopped with changes in it offers them."""
    r = builder_page.run(f"""
    await advance(10);
    {before}
    {ending}
    """ + NOTICE, SAVED_DARK)
    assert r["shown"] is True
    assert r["offer"] is offer


def test_a_superseded_tab_with_nothing_unsaved_still_tries_to_close():
    r = builder_page.run("await advance(10);" + _NEWER + NOTICE, SAVED_DARK)
    assert r["closed"] == 1
    assert r["shown"] is True       # what stays when the browser refuses


def test_a_superseded_tab_with_a_draft_does_not_close():
    r = builder_page.run("await advance(10);" + _OCEAN + _NEWER + NOTICE,
                         SAVED_DARK)
    assert r["closed"] == 0 and r["offer"] is True


def test_the_notices_copy_button_copies_the_share_code():
    r = builder_page.run("""
    await advance(10);
    pick('c-high', '#ff0000');
    await advance(300);
    """ + _NEWER + """
    $('notice-copy').click();
    await flush();
    return { wrote: clipboard.writes, box: $('code').dataset.plain,
             label: $('notice-copy').querySelector('.lbl').textContent };
    """, SAVED_DARK)
    assert r["box"].startswith("blink theme set BT1.")
    assert r["wrote"] == [r["box"]]
    assert r["label"] == "Copied"


def test_focus_stays_inside_the_notice():
    r = builder_page.run("""
    await advance(10);
    """ + _OCEAN + _NEWER + """
    const d = $('notice-dialog'), at = [];
    for (let i = 0; i < 3; i++) {
      const e = document.fire('keydown', { key: 'Tab' });
      at.push([document.activeElement === d ? 'dialog' : document.activeElement.id, !!e.prevented]);
    }
    $('save').focus();
    document.fire('focusin', { target: $('save') });
    return { at, back: document.activeElement === d };
    """, SAVED_DARK)
    assert r["at"] == [["notice-copy", True], ["dialog", True],
                       ["notice-copy", True]]
    assert r["back"] is True


_SAVE = "$('save').click(); await advance(10);"
_CANCEL = "$('cancel').click(); await advance(10);"
_KEEP = "notice-why notice-keep-text notice-end"
_PLAIN = "notice-why notice-end"


@pytest.mark.parametrize("before, ending, described", [
    ("", _SAVE, _PLAIN), (_OCEAN, _SAVE, _PLAIN),
    (_OCEAN, _CANCEL, _PLAIN), ("", _NEWER, _PLAIN),
    (_OCEAN, _NEWER, _KEEP), ("", _NO_ANSWER, _PLAIN),
    (_OCEAN, _NO_ANSWER, _KEEP),
], ids=["saved", "saved, after a change", "cancelled", "newer, none",
        "newer, draft", "stopped, none", "stopped, draft"])
def test_the_dialog_is_described_by_only_what_it_shows(before, ending,
                                                       described):
    """M1: the description named the "not saved" line in every case, and a
    description reads an element it names even while it is hidden, so
    Chromium's accessibility tree read a saved tab as the saved sentence
    (then "The theme was saved.") followed by "Your changes here are not
    saved." """
    r = builder_page.run(f"""
    await advance(10);
    {before}
    {ending}
    """ + NOTICE, SAVED_DARK)
    assert r["described"] == described


def test_a_reply_landing_after_the_ending_does_not_replace_it():
    """S1: a ping's reply held back past Save's, then saying a newer tab
    took over, reaches superseded() after the tab has already ended as
    saved. Only the guard in retire() keeps the notice saying saved."""
    r = builder_page.run("""
    await advance(10);
    preset('Ocean'); await advance(300);
    const h = hold();
    answer('/api/ping', () => h.p);
    await advance(19700);                       // the ping is out, held
    $('save').click(); await advance(10);       // saved
    h.release({ status: 200, body: { ok: true, newer: true } }); await advance(10);
    """ + NOTICE, SAVED_DARK)
    assert (r["title"], r["why"]) == SAVED
    assert r["offer"] is False
    assert r["status"].startswith("Saved: Ocean.")


# Save held from t = 19610, just before the 20 s ping. The ping and its
# retry 3 s later both miss, so a stop is owed while Save is still out,
# well inside Save's 10 s deadline.
_OWED = """
    await advance(10);
    preset('Ocean'); await advance(300);
    await advance(19300);
    const s = hold();
    answer('/api/save', () => s.p);
    answer('/api/ping', () => 'fail');
    $('save').click(); await advance(10);
    await advance(3500);
    const during = $('notice').getAttribute('hidden') !== null;
"""


def test_a_save_in_flight_outranks_a_failure_that_lands_first():
    """N3: a failure settling before Save's reply said "stopped" over a save
    that the server had stored."""
    r = builder_page.run(_OWED + """
    s.release({ status: 200, body: { ok: true } }); await advance(10);
    """ + NOTICE.replace("return {", "return { during,"), SAVED_DARK)
    assert r["during"] is True                  # no notice while Save is out
    assert (r["title"], r["why"]) == SAVED


def test_a_save_that_never_answers_lets_a_held_stop_through():
    """R2-MUST: a Save stuck inside the server held every stop back for
    good. After the server's own handler timeout the stop shows."""
    r = builder_page.run(_OWED + """
    const before = $('notice').getAttribute('hidden') !== null;
    await advance(7000);                        // past t = 29610
    """ + NOTICE.replace("return {", "return { during, before,"), SAVED_DARK)
    assert r["during"] is True and r["before"] is True
    assert r["shown"] is True and (r["title"], r["why"]) == STOPPED
    assert r["offer"] is True


def test_a_save_that_never_answers_with_nothing_owed_asks_a_ping():
    r = builder_page.run("""
    await advance(10);
    preset('Ocean'); await advance(300);
    answer('/api/save', () => hold().p);
    $('save').click(); await advance(10);
    const pings = () => requests.filter(q => q.path === '/api/ping').length;
    const at = pings();
    await advance(10100);
    return [at, pings(), $('save').disabled, $('notice').getAttribute('hidden') !== null];
    """, SAVED_DARK)
    assert r == [1, 2, False, True]   # asked; the builder is up; Save again


def test_a_save_refused_with_an_error_says_why_and_keeps_the_tab():
    """R2-S1: Save answering 500 proves the builder is up, but a stop owed
    from before still won, sending the owner to restart a builder that was
    running and never saying why the save failed."""
    r = builder_page.run(_OWED + """
    answer('/api/ping', () => ({ status: 200, body: { ok: true } }));
    s.release({ status: 500, body: { error: 'Not saved: this computer could not write the theme file.' } });
    await advance(10);
    await advance(3500);
    """ + NOTICE.replace("return {", "return { during, save: $('save').disabled,"), SAVED_DARK)
    assert r["during"] is True
    assert r["shown"] is False
    assert r["status"] == "Not saved: this computer could not write the theme file."
    assert r["save"] is False and "paste" in r["live"]


def test_a_save_refused_with_an_error_then_a_dead_builder_stops():
    r = builder_page.run(_OWED + """
    s.release({ status: 500, body: { error: 'Not saved: no.' } });
    await advance(10);
    const said = $('status-text').textContent;
    await advance(3500);                        // the ping and its retry miss
    """ + NOTICE.replace("return {", "return { said,"), SAVED_DARK)
    assert r["said"] == "Not saved: no."
    assert r["shown"] is True and (r["title"], r["why"]) == STOPPED


def test_one_failed_preview_asks_a_ping_rather_than_stopping():
    """R2-N1: one dropped preview retired a live tab at once."""
    r = builder_page.run("""
    await advance(10);
    let n = 0;
    answer('/api/preview', () => (++n === 1 ? 'fail' : { status: 200, body: { ok: true } }));
    preset('Ocean'); await advance(300);
    const pings = requests.filter(q => q.path === '/api/ping').length;
    preset('Forest'); await advance(300);
    """ + NOTICE.replace("return {", "return { pings,"), SAVED_DARK)
    assert r["pings"] == 2            # the one at load, and the one asked
    assert r["shown"] is False
    assert r["status"].startswith("Preview sent: Forest")


def test_save_is_off_while_one_is_out():
    """R2-N2: a second click shared the first's state."""
    r = builder_page.run("""
    await advance(10);
    preset('Ocean'); await advance(300);
    const s = hold();
    answer('/api/save', () => s.p);
    $('save').click(); await advance(10);
    const during = $('save').disabled;
    pick('c-high', '#ff0000'); await advance(10);   // a render: still off
    const rendered = $('save').disabled;
    $('save').click(); await advance(10);
    const saves = requests.filter(q => q.path === '/api/save').length;
    s.release({ status: 400, body: { error: 'Refused.' } }); await advance(10);
    return [during, rendered, saves, $('save').disabled];
    """, SAVED_DARK)
    assert r == [True, True, 1, False]


def test_one_missed_ping_is_asked_again_before_the_tab_stops():
    """N2: one dropped request on loopback ended a live tab for good."""
    r = builder_page.run("""
    await advance(10);
    let n = 0;
    answer('/api/ping', () => (++n === 1 ? 'fail' : { status: 200, body: { ok: true } }));
    await advance(20000);
    const after1 = $('notice').getAttribute('hidden') !== null;
    await advance(3100);
    const pings = requests.filter(q => q.path === '/api/ping').length;
    """ + NOTICE.replace("return {", "return { after1, pings,"), SAVED_DARK)
    assert r["after1"] is True
    assert r["pings"] == 3            # load, the miss, its retry
    assert r["shown"] is False and r["live"]


def test_two_missed_pings_stop_the_tab():
    r = builder_page.run("""
    await advance(10);
    answer('/api/ping', () => 'fail');
    await advance(20000);
    const early = $('notice').getAttribute('hidden') === null;
    await advance(3100);
    """ + NOTICE.replace("return {", "return { early,"), SAVED_DARK)
    assert r["early"] is False
    assert r["shown"] is True and (r["title"], r["why"]) == STOPPED


def test_a_tab_back_from_the_back_forward_cache_asks_at_once():
    """S2: such a tab sent /api/closed as it left, and the builder may have
    ended since."""
    r = builder_page.run("""
    await advance(10);
    const count = () => requests.filter(q => q.path === '/api/ping').length;
    const at = count();
    (window.listeners.pageshow || []).forEach(f => f({ persisted: false }));
    await flush();
    const fresh = count();
    (window.listeners.pageshow || []).forEach(f => f({ persisted: true }));
    await flush();
    return [at, fresh, count()];
    """, SAVED_DARK)
    assert r == [1, 1, 2]


def test_a_refused_clipboard_shows_the_code_in_the_notice_selected():
    """S3: the code's own box is behind the notice and inert, so a refused
    copy shows the code in the card and selects it there."""
    r = builder_page.run("""
    await advance(10);
    """ + _OCEAN + _NEWER + """
    clipboard.refuse = true;
    const before = $('notice-code').getAttribute('hidden') !== null;
    $('notice-copy').click();
    await flush();
    return { before, shown: $('notice-code').getAttribute('hidden') === null,
             text: $('notice-code').textContent, box: $('code').dataset.plain,
             selected: selection.node === $('notice-code'),
             label: $('notice-copy').querySelector('.lbl').textContent };
    """, SAVED_DARK)
    assert r["before"] is True and r["shown"] is True
    assert r["text"] == r["box"] and r["text"].startswith("blink theme set")
    assert r["selected"] is True
    assert r["label"] == "Selected"


def test_a_tab_with_nothing_to_copy_is_not_told_to_copy():
    """N1: a tab superseded before it heard its saved theme said "copy the
    share code" in the bar while its notice offered none. It still does
    not close, since it cannot know it holds nothing. The newer tab is
    heard from a ping asked on coming back into view, inside the 10 s that
    /api/state is now given (R4-N2); the 20 s timer's ping came after it,
    when the unanswered state had already ended the tab."""
    r = builder_page.run("""
    await advance(10);
    answer('/api/ping', () => ({ status: 200, body: { ok: true, newer: true } }));
    document.fire('visibilitychange'); await advance(10);
    """ + NOTICE, {"/api/state": "hold"})
    assert r["status"] == "Continued in a newer tab. You can close this one."
    assert r["offer"] is False
    assert r["closed"] == 0


def test_the_page_writes_no_markup_from_script():
    """The notice's words, like every other word the script writes, go in
    by textContent."""
    _, script = builder_page.page_parts()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                 "document.write"):
        assert sink not in script, sink


def test_the_notice_fades_only_for_those_who_allow_motion():
    page = theme_server.page()
    css = re.sub(r"/\*.*?\*/", "",
                 page[page.index("<style>"):page.index("</style>")], flags=re.S)
    assert re.search(r"\.notice\s*\{[^}]*animation:\s*notice-in", css)
    reduced = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\}\s*\}", css,
        re.S)
    assert reduced and "animation: none !important" in reduced.group(1)


def test_a_preview_that_fails_with_the_builder_up_says_so():
    """R3-S1: a preview failing on a full disk asked a ping, the ping came
    back alive, and the bar went on saying the board showed each change.
    The theme is still unsent, so choosing it again sends it again."""
    r = builder_page.run("""
    await advance(10);
    answer('/api/preview', () => 'fail');
    preset('Ocean'); await advance(300);
    const said = $('status-text').textContent, bad = $('status').classList.contains('err');
    answer('/api/preview', () => ({ status: 200, body: { ok: true } }));
    preset('Ocean'); await advance(300);
    return { said, bad, notice: $('notice').getAttribute('hidden') === null,
             previews: requests.filter(q => q.path === '/api/preview').length,
             now: $('status-text').textContent };
    """, SAVED_DARK)
    assert r["said"] == "The preview did not reach your board."
    assert r["bad"] is True and r["notice"] is False
    assert r["previews"] == 2
    assert r["now"].startswith("Preview sent: Ocean")


def test_a_change_made_while_save_is_out_stays_unsaved():
    """R3-N2: savedKey took the page as it was when Save's reply came, so
    a colour changed in between counted as saved, and was never offered."""
    r = builder_page.run("""
    await advance(10);
    preset('Ocean'); await advance(300);
    const s = hold();
    answer('/api/save', () => s.p);
    $('save').click(); await advance(10);
    const sent = requests.filter(q => q.path === '/api/save')[0].body;
    pick('c-high', '#ff0000'); await advance(10);
    s.release({ status: 200, body: { ok: true } }); await advance(10);
    $('notice-copy').click(); await flush();
    """ + NOTICE.replace("return {", "return { sent, copied: clipboard.writes,"), SAVED_DARK)
    assert r["sent"]["id"] != 12      # Ocean, a built-in, went to the server
    assert (r["title"], r["why"]) == SAVED
    assert r["offer"] is True
    assert r["described"] == _KEEP
    # Under "Saved." the usual "Your changes here are not saved." read as a
    # contradiction at a glance (review of fp/notice-title, NICE-3): this
    # line says which changes it means.
    assert r["keep"] == ("Changes you made after saving are not on the board."
                         " Copy the share code to keep them.")
    assert len(r["copied"]) == 1 and r["copied"][0].startswith("blink theme set BT1.")


@pytest.mark.parametrize("ending", [_NEWER, _NO_ANSWER],
                         ids=["newer", "stopped"])
def test_changes_that_were_never_saved_are_offered_as_not_saved(ending):
    r = builder_page.run(f"""
    await advance(10);
    {_OCEAN}
    {ending}
    """ + NOTICE, SAVED_DARK)
    assert r["offer"] is True
    assert r["keep"] == ("Your changes here are not saved. Copy the share"
                         " code to keep them.")


def test_a_server_that_never_answers_ends_in_the_notice():
    """R3-N3: a server that takes the connection and never answers held
    every ping forever, so the tab looked live for good. Each ping is given
    up on after 10 s, then its retry, then the notice."""
    r = builder_page.run("""
    await advance(10);
    preset('Ocean'); await advance(300);
    answer('/api/ping', () => hold().p);
    await advance(29000);                       // the 20 s ping, 9 s in
    const at29 = $('notice').getAttribute('hidden') !== null;
    await advance(16000);                       // abort, retry, abort
    const limited = requests.filter(q => q.path === '/api/ping').every(q => q.limited);
    """ + NOTICE.replace("return {", "return { at29, limited,"), SAVED_DARK)
    assert r["at29"] is True
    assert r["limited"] is True
    assert r["shown"] is True and (r["title"], r["why"]) == STOPPED
    assert r["offer"] is True


def test_a_save_is_not_given_up_on_by_the_request_limit():
    """Save keeps its own deadline, and its answer still counts after it:
    one stuck 15 s in the server still saved, in Chromium."""
    r = builder_page.run("""
    await advance(10);
    preset('Ocean'); await advance(300);
    const s = hold();
    answer('/api/save', () => s.p);
    $('save').click(); await advance(15000);
    s.release({ status: 200, body: { ok: true } }); await advance(10);
    """ + NOTICE, SAVED_DARK)
    assert (r["title"], r["why"]) == SAVED


def test_saving_shows_until_a_late_answer():
    """R3-N4: between Save's deadline and a late answer, Save was back on
    and the bar said something else, as if the click had been lost."""
    r = builder_page.run("""
    await advance(10);
    preset('Ocean'); await advance(300);
    const s = hold();
    answer('/api/save', () => s.p);
    $('save').click(); await advance(10);
    const click = $('status-text').textContent;
    preset('Forest'); await advance(300);       // a preview says its piece
    const preview = $('status-text').textContent;
    await advance(10000);                       // past the deadline
    const late = [$('status-text').textContent, $('save').disabled];
    s.release({ status: 200, body: { ok: true } }); await advance(10);
    return { click, preview, late,
             end: [$('notice-title').textContent, $('notice-why').textContent] };
    """, SAVED_DARK)
    assert r["click"] == "Saving…"
    assert r["preview"].startswith("Preview sent: Forest")
    assert r["late"] == ["Saving…", False]
    assert r["end"] == list(SAVED)


def test_a_late_ping_from_a_failed_preview_does_not_undo_a_newer_one():
    """R4-N1, probe A: preview A failed and the ping it asked was slow;
    preview B went through; then A's ping came back alive and wrote "did
    not reach your board" over B's "Preview sent", though the board showed
    B. A's news is older than B's, so B's stands."""
    r = builder_page.run("""
    await advance(10);
    answer('/api/preview', () => 'fail');
    const slow = hold();
    answer('/api/ping', () => slow.p);
    preset('Ocean'); await advance(300);        // A fails, its ping is out
    answer('/api/preview', () => ({ status: 200, body: { ok: true } }));
    preset('Forest'); await advance(300);       // B goes through
    const sent = $('status-text').textContent;
    slow.release({ status: 200, body: { ok: true } }); await advance(10);
    return { sent, now: $('status-text').textContent,
             bad: $('status').classList.contains('err') };
    """, SAVED_DARK)
    assert r["sent"].startswith("Preview sent: Forest")
    assert r["now"] == r["sent"]
    assert r["bad"] is False


def test_a_late_ping_still_says_unsent_when_nothing_newer_went_through():
    """The other side of R4-N1: a slow ping with no preview through since
    still says the failed one did not reach the board."""
    r = builder_page.run("""
    await advance(10);
    answer('/api/preview', () => 'fail');
    const slow = hold();
    answer('/api/ping', () => slow.p);
    preset('Ocean'); await advance(300);
    preset('Forest'); await advance(300);       // fails too, no newer news
    slow.release({ status: 200, body: { ok: true } }); await advance(10);
    return $('status-text').textContent;
    """, SAVED_DARK)
    assert r == "The preview did not reach your board."


def test_a_state_that_never_answers_ends_in_the_notice():
    """R4-N2, probe E: a GET /api/state that never answered left the bar at
    "Loading your current theme." for good. It is given up on after 10 s,
    the same limit the posts have, and ends as a refused one does."""
    r = builder_page.run("""
    await advance(9000);
    const at9 = [$('notice').getAttribute('hidden') !== null,
                 $('status-text').textContent];
    await advance(1100);
    const limited = requests.filter(q => q.path === '/api/state').every(q => q.limited);
    """ + NOTICE.replace("return {", "return { at9, limited,"),
                         {"/api/state": "hold"})
    assert r["at9"] == [True, "Loading your current theme. You can make"
                       " changes once it is here."]
    assert r["limited"] is True
    assert r["shown"] is True and (r["title"], r["why"]) == STOPPED
    assert r["offer"] is False      # it never had the owner's theme


# ------------------------------------------- nothing is sent before the state
#
# NN-3 of the review of fp/tab-notice-nice, probe P5: Save was live before
# GET /api/state answered, and a tab that never read the owner's theme saved
# the page's own default over it ("Saved: Dark"). A preview from then reached
# the board too, and the state landing next threw the edit away under a bar
# that said the board showed the saved theme. The state here is "held", so
# it answers when the test says, and the owner's theme is Synthwave, so a
# save of Dark, the page's default, cannot pass for it.

_RELEASE_SYNTHWAVE = ("held['/api/state'][0].release({ status: 200, body: "
                      "{ saved: { id: 5 }, draft: null } }); await advance(10);")
_FORM = "document.querySelectorAll('main button, main input, #copy-code')"


def test_save_is_off_until_the_state_answers():
    r = builder_page.run("""
    await advance(10);
    const before = $('save').disabled;
    $('save').fire('click');     // a click that arrives anyway is ignored
    await advance(10);
    const early = requests.filter(q => q.path === '/api/save').length;
    """ + _RELEASE_SYNTHWAVE + """
    const after = $('save').disabled;
    $('save').click(); await advance(10);
    return { before, early, after, title: $('notice-title').textContent,
             why: $('notice-why').textContent,
             sent: requests.filter(q => q.path === '/api/save').map(q => q.body) };
    """, {"/api/state": "held"})
    assert r["before"] is True and r["early"] == 0
    assert r["after"] is False
    assert r["sent"] == [{"id": 5}]
    assert (r["title"], r["why"]) == SAVED


def test_nothing_is_previewed_before_the_state_answers():
    """A preset click and a typed colour before the state are not sent, and
    the form is off and says it is waiting until the state is here."""
    r = builder_page.run("""
    await advance(10);
    const off = """ + _FORM + """.every(e => e.disabled);
    const busy = document.querySelector('main').getAttribute('aria-busy');
    const ocean = document.querySelectorAll('#presets button').find(b => b.querySelector('.pn').textContent === 'Ocean');
    ocean.fire('click');         // arriving past disabled, as Save's does
    $('t-low').value = '#123456'; $('t-low').fire('input');
    await advance(300);
    const early = requests.filter(q => q.path === '/api/preview').length;
    """ + _RELEASE_SYNTHWAVE + """
    const on = """ + _FORM + """.every(e => !e.disabled);
    const busyAfter = document.querySelector('main').getAttribute('aria-busy');
    const bar = $('status-text').textContent;
    preset('Ocean'); await advance(300);
    return { off, busy, early, on, busyAfter, bar,
             previews: requests.filter(q => q.path === '/api/preview').map(q => q.body) };
    """, {"/api/state": "held"})
    assert r["off"] is True and r["busy"] == "true"
    assert r["early"] == 0
    assert r["on"] is True and r["busyAfter"] is None
    assert r["bar"] == ("This is the theme your board has now. Changes show"
                        " on it as a preview.")
    assert r["previews"] == [{"id": 4}]


@pytest.mark.parametrize("ending", [
    "held['/api/state'][0].release({ status: 500, body: {} }); await advance(10);",
    "await advance(10100);"], ids=["refused", "never answers"])
def test_a_tab_whose_state_never_came_never_turns_save_on(ending):
    """Stopped before the owner's theme was here: Save stays off, and a
    click that arrives anyway sends nothing."""
    r = builder_page.run("""
    await advance(10);
    const early = $('save').disabled;
    """ + ending + """
    $('save').fire('click'); await advance(10);
    return { early, now: $('save').disabled, title: $('notice-title').textContent,
             why: $('notice-why').textContent,
             saves: requests.filter(q => q.path === '/api/save').length };
    """, {"/api/state": "held"})
    assert r["early"] is True and r["now"] is True
    assert (r["title"], r["why"]) == STOPPED
    assert r["saves"] == 0


def test_a_tab_taken_over_while_its_state_was_out_stays_off():
    """The state landing after the tab has ended does not turn its form
    back on behind the notice."""
    r = builder_page.run("""
    await advance(10);
    const early = $('save').disabled;
    answer('/api/ping', () => ({ status: 200, body: { ok: true, newer: true } }));
    document.fire('visibilitychange'); await advance(10);
    """ + _RELEASE_SYNTHWAVE + NOTICE.replace("return {", "return { early,"),
                         {"/api/state": "held"})
    assert r["early"] is True
    assert (r["title"], r["why"]) == SUPERSEDED
    assert r["live"] == []


def test_a_handler_dispatched_past_disabled_before_the_state_does_nothing():
    """Review of fp/save-before-load, N1 and S1: every control that
    changes the theme or copies it is fired by script before the state,
    past its disabled, the way an extension could. With saved: null the
    state leaves the page's colours as they are, so any change that got in
    would survive it and be what Save writes (probe P7), and a share code
    loaded then kept its "Loaded." over a page that no longer showed it
    (P6). Each handler is fired alone in effect: all the others are
    guarded, so any one unguarded changes what Save sends, the message
    under the share box, or the clipboard."""
    r = builder_page.run(f"""
    await advance(10);
    const by = (sel, key, v) => document.querySelectorAll(sel).find(b => b.dataset[key] === v);
    document.querySelectorAll('#presets button').find(b => b.querySelector('.pn').textContent === 'Ocean').fire('click');
    $('c-low').value = '#123456'; $('c-low').fire('input');
    $('t-mid').value = '#654321'; $('t-mid').fire('input');
    by('[data-m]', 'm', 'gauge').fire('click');
    by('[data-w]', 'w', '22').fire('click');
    by('[data-e]', 'e', 'flat').fire('click');
    by('[data-d]', 'd', 'off').fire('click');
    $('paste').value = '{_code(["4387df", "0da243", "0da243", "ba8107", "ff1900"], text="e6e8eb", bg="0e1116")}';
    $('load').fire('click');
    $('copy-cmd').fire('click'); $('copy-code').fire('click');
    await advance(300);
    held['/api/state'][0].release({{ status: 200, body: {{ saved: null, draft: null }} }}); await advance(10);
    const paste = $('m-paste').textContent, copied = clipboard.writes.slice();
    $('save').click(); await advance(10);
    return {{ paste, copied, sent: requests.filter(q => q.path === '/api/save').map(q => q.body),
             previews: requests.filter(q => q.path === '/api/preview').length }};
    """, {"/api/state": "held"})
    assert r["sent"] == [{"id": 0}]    # the page's default, untouched
    assert r["paste"] == ""
    assert r["copied"] == []
    assert r["previews"] == 0


def test_the_controls_dim_only_once_a_state_is_slow():
    """N3: dimmed from the first frame, a state answered on loopback in a
    few milliseconds flashed the controls dim. They dim at 150 ms, and
    come back when the state lands."""
    r = builder_page.run("""
    const cls = () => document.querySelector('main').classList.contains('waiting');
    await advance(100);
    const at100 = cls();
    await advance(100);
    const at200 = cls();
    """ + _RELEASE_SYNTHWAVE + """
    return { at100, at200, after: cls() };
    """, {"/api/state": "held"})
    assert r == {"at100": False, "at200": True, "after": False}


def test_a_tab_that_ends_before_the_dim_never_dims():
    """Review of fp/save-before-load r2, NICE 1: the 150 ms timer outlived
    a state refused at 10 ms, and dimmed the controls behind the notice.
    gone() ends it with the tab."""
    r = builder_page.run("""
    const cls = () => document.querySelector('main').classList.contains('waiting');
    await advance(10);
    held['/api/state'][0].release({ status: 500, body: {} }); await advance(10);
    const shown = $('notice').getAttribute('hidden') === null;
    await advance(300);
    return { shown, dim: cls() };
    """, {"/api/state": "held"})
    assert r == {"shown": True, "dim": False}


def test_a_tab_that_ends_after_the_dim_takes_it_off():
    """Review of fp/docs-pass, N8: a state slower than 150 ms and then
    refused left the dim on, and main marked busy, behind the notice. The
    tab has ended rather than still loading, so gone() takes both off."""
    r = builder_page.run("""
    const main = document.querySelector('main');
    await advance(200);
    const before = [main.classList.contains('waiting'), main.getAttribute('aria-busy')];
    held['/api/state'][0].release({ status: 500, body: {} }); await advance(10);
    return { before, dim: main.classList.contains('waiting'),
             busy: main.getAttribute('aria-busy'),
             shown: $('notice').getAttribute('hidden') === null };
    """, {"/api/state": "held"})
    assert r == {"before": [True, "true"], "dim": False, "busy": None,
                 "shown": True}


def test_the_loading_dim_leaves_the_words_readable():
    """N2: opacity .6 on the whole studio put the dim text, the lede and
    each description, at 2.6 to 2.9:1 in both schemes. The dim is on the
    controls alone, and never on .btn, which has its own disabled look."""
    page = theme_server.page()
    css = page[page.index("<style>"):page.index("</style>")]
    busy = [(sels, d) for sels, d in _rules(css)
            if any("waiting" in s or "aria-busy" in s for s in sels)]
    assert busy
    for sels, d in busy:
        for s in sels:
            assert re.search(r"(button:not\(\.btn\)|input)$", s), s
