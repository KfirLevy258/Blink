"""The sessions card's rows: one per open session, in the order the card
shows them (docs/sessions-card-design.md, issue #18).

Pure on purpose. It takes what the providers already read -- the hook slots'
per-session details (claude_state.session_details, codex_state.details) and
the status line's per-session context (claude_cli.context_by_session) -- and
returns plain values. It reads no file, holds no state and logs nothing, so
importing it does nothing and the Bus can call it on the serial thread every
poll for the price of a sort.

The wire is protocol.srow's; this module never builds a message, so a row's
name reaches the board only through protocol's byte clip.
"""
from collections import namedtuple

from pc.providers import base

# One row. `provider` is the wire's "c" (Claude) or "x" (Codex); `name` the
# project directory's final segment or "" when the slot carried none; `kind`
# one of KINDS; `age_s` whole seconds, never negative; `ctx` the context
# fullness 0..100, or None when unknown -- never 0 for unknown, because a bar
# at 0% is a reading and nobody took one.
Row = namedtuple("Row", "provider name kind age_s ctx")

PROVIDER_CLAUDE = "c"
PROVIDER_CODEX = "x"

KIND_WAIT = "wait"
KIND_FAIL = "fail"
KIND_EDIT = "edit"
KIND_TOOL = "tool"
KIND_WORK = "work"
KIND_IDLE = "idle"
KINDS = (KIND_WAIT, KIND_FAIL, KIND_EDIT, KIND_TOOL, KIND_WORK, KIND_IDLE)

# The tools whose work is changing files, which the card says as "Editing".
# Claude Code's own four, and Codex's apply_patch. Any other tool in flight is
# "Running a tool"; a tool name this list does not know is never guessed at.
#
# The shim does this classing itself and writes only the class, "edit" or
# "other" (tools/blink-hook.sh: the tool's name never reaches the state
# directory, which tests/ci/check_hook_shim.sh holds it to). So a slot's
# `tool` is normally TOOL_CLASS_EDIT or anything else; the names themselves
# are still accepted, so the mapping reads the same whichever side classed it.
TOOL_CLASS_EDIT = "edit"
EDIT_TOOLS = frozenset({TOOL_CLASS_EDIT, "Edit", "Write", "MultiEdit",
                        "NotebookEdit", "apply_patch"})

# Rows per page: four 44 px rows fit between the 40 px bar and the 24 px page
# strip on 320x240 (the design's "What a row says").
PAGE_SIZE = 4

# Sort groups: needs-you first, then failed, then everything running as one
# group (an edit is not more urgent than a tool call), then idle.
_GROUP = {KIND_WAIT: 0, KIND_FAIL: 1, KIND_EDIT: 2, KIND_TOOL: 2,
          KIND_WORK: 2, KIND_IDLE: 3}


def kind_of(state, tool, event):
    """The card's word for one session, or None for a state it does not show.

    `stuck` is filed with failed: no provider produces it any more
    (claude_state's docstring), and the protocol still names it, so a
    session that somehow carries it is shown as the red it always was.
    Unknown states are skipped rather than guessed -- the same rule the
    census applies.
    """
    if state == base.STATE_WAITING:
        return KIND_WAIT
    if state in (base.STATE_FAILED, base.STATE_STUCK):
        return KIND_FAIL
    if state == base.STATE_RUNNING:
        # Only PreToolUse has a tool IN FLIGHT. The shim records the name on
        # that event alone, so a tool beside any other event is a slot
        # written by something else and is not believed.
        if event == "PreToolUse":
            return KIND_EDIT if tool in EDIT_TOOLS else KIND_TOOL
        return KIND_WORK
    if state == base.STATE_IDLE:
        return KIND_IDLE
    return None


def _age(value):
    try:
        age = float(value)
    except (TypeError, ValueError):
        return 0
    if age != age or age < 0:        # NaN, or a clock that stepped back
        return 0
    if age == float("inf"):
        return 0
    return int(age)


def _rows_for(provider, details, ctx_by_sid):
    out = []
    if not isinstance(details, dict):
        return out
    for sid, d in details.items():
        try:
            state, name, age_s, tool, event = d
        except (TypeError, ValueError):
            continue
        kind = kind_of(state, tool, event)
        if kind is None:
            continue
        ctx = None
        if provider == PROVIDER_CLAUDE:
            v = ctx_by_sid.get(sid)
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100:
                ctx = v
        out.append(Row(provider, name if isinstance(name, str) else "",
                       kind, _age(age_s), ctx))
    return out


def build_rows(claude_details, codex_details, ctx_by_sid):
    """Every live session as a Row, in the card's order. NO cap: the card
    shows PAGE_SIZE at a time and pages through the rest (page()).

    Sorted needs-you, failed, running (edit, tool and work together), idle;
    newest (smallest age) first inside each group; stable, so two rows that
    tie keep the order their providers gave them. Context is attached to
    Claude rows only: it is keyed by a Claude Code session id, and a Codex
    row has no bar in this version.
    """
    ctx = ctx_by_sid if isinstance(ctx_by_sid, dict) else {}
    rows = (_rows_for(PROVIDER_CLAUDE, claude_details, ctx)
            + _rows_for(PROVIDER_CODEX, codex_details, ctx))
    rows.sort(key=lambda r: (_GROUP[r.kind], r.age_s))
    return rows


def page(rows, p, size=PAGE_SIZE):
    """(page, rows on it, pages) for page `p` of `rows`.

    `p` is clamped into the pages that exist, so a board asking for page 5
    of a list that shrank to one page is shown page 0's rows and told it is
    on page 0. There is always at least one page: an empty list is one empty
    page, which is how the card says nobody is running anything.
    """
    rows = list(rows)
    n_pages = max(1, (len(rows) + size - 1) // size)
    if isinstance(p, bool) or not isinstance(p, int):
        p = 0
    p = min(max(p, 0), n_pages - 1)
    return p, rows[p * size:(p + 1) * size], n_pages
