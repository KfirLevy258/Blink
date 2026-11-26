"""pc/session_rows: the sessions card's rows, their order and their pages.

Generated data only: every detail tuple here is written by this file, in the
shape claude_state.session_details returns.
"""
from pc import session_rows
from pc.providers import base


def _d(state, name="proj", age=10, tool="", event="UserPromptSubmit"):
    return (state, name, age, tool, event)


def test_each_state_maps_to_its_kind():
    claude = {
        "w": _d(base.STATE_WAITING, event="PermissionRequest"),
        "f": _d(base.STATE_FAILED, event="StopFailure"),
        "s": _d(base.STATE_STUCK),
        "e": _d(base.STATE_RUNNING, tool="Edit", event="PreToolUse"),
        "t": _d(base.STATE_RUNNING, tool="Bash", event="PreToolUse"),
        "r": _d(base.STATE_RUNNING, event="PostToolUse"),
        "i": _d(base.STATE_IDLE, event="Stop"),
        "u": _d(base.STATE_UNKNOWN, event="SessionStart"),
    }
    kinds = sorted(r.kind for r in session_rows.build_rows(claude, {}, {}))
    assert kinds == sorted(["wait", "fail", "fail", "edit", "tool", "work",
                            "idle"])


def test_every_edit_tool_is_editing_and_others_are_a_tool():
    # "edit" is the class the shim writes; the raw names are still accepted.
    for tool in ("edit", "Edit", "Write", "MultiEdit", "NotebookEdit",
                 "apply_patch"):
        assert session_rows.kind_of(base.STATE_RUNNING, tool,
                                    "PreToolUse") == "edit"
    for tool in ("other", "Bash", "Read", "", None, "mcp__x__Edit"):
        assert session_rows.kind_of(base.STATE_RUNNING, tool,
                                    "PreToolUse") == "tool"


def test_a_tool_beside_another_event_is_working_not_a_tool():
    # The shim records the tool on PreToolUse alone; a finished tool is not
    # "Editing".
    assert session_rows.kind_of(base.STATE_RUNNING, "Edit",
                                "PostToolUse") == "work"
    assert session_rows.kind_of(base.STATE_RUNNING, "",
                                "UserPromptSubmit") == "work"


def test_an_old_slot_with_no_tool_reads_as_working():
    rows = session_rows.build_rows(
        {"a": _d(base.STATE_RUNNING, tool="", event="UserPromptSubmit")}, {},
        {})
    assert [r.kind for r in rows] == ["work"]


def test_needs_you_first_then_failed_then_running_then_idle():
    claude = {
        "idle": _d(base.STATE_IDLE, name="I", age=1),
        "run": _d(base.STATE_RUNNING, name="R", age=2),
        "fail": _d(base.STATE_FAILED, name="F", age=3),
        "wait": _d(base.STATE_WAITING, name="W", age=400),
    }
    rows = session_rows.build_rows(claude, {}, {})
    assert [r.name for r in rows] == ["W", "F", "R", "I"]


def test_newest_first_within_a_group_and_running_kinds_share_one():
    claude = {
        "a": _d(base.STATE_RUNNING, name="old-work", age=300),
        "b": _d(base.STATE_RUNNING, name="new-edit", age=5, tool="Write",
                event="PreToolUse"),
        "c": _d(base.STATE_RUNNING, name="mid-tool", age=60, tool="Bash",
                event="PreToolUse"),
    }
    codex = {"x": _d(base.STATE_WAITING, name="cx-wait", age=900),
             "y": _d(base.STATE_WAITING, name="cx-wait-new", age=9)}
    rows = session_rows.build_rows(claude, codex, {})
    assert [r.name for r in rows] == ["cx-wait-new", "cx-wait", "new-edit",
                                      "mid-tool", "old-work"]
    assert [r.provider for r in rows[:2]] == ["x", "x"]


def test_nine_sessions_are_all_reachable_across_three_pages():
    claude = {f"s{i}": _d(base.STATE_RUNNING, name=f"p{i}", age=i)
              for i in range(9)}
    rows = session_rows.build_rows(claude, {}, {})
    assert len(rows) == 9               # no cap
    seen = []
    for p in range(3):
        pg, on_page, pages = session_rows.page(rows, p)
        assert pg == p and pages == 3
        seen.extend(r.name for r in on_page)
    assert seen == [f"p{i}" for i in range(9)]
    assert len(session_rows.page(rows, 2)[1]) == 1


def test_a_page_past_the_end_is_clamped_to_the_last():
    rows = session_rows.build_rows(
        {f"s{i}": _d(base.STATE_IDLE, age=i) for i in range(5)}, {}, {})
    assert session_rows.page(rows, 99)[0] == 1
    assert session_rows.page(rows, -3)[0] == 0
    assert session_rows.page(rows, True)[0] == 0
    assert session_rows.page(rows, "1")[0] == 0


def test_an_empty_list_is_one_empty_page():
    assert session_rows.page([], 0) == (0, [], 1)
    assert session_rows.page([], 7) == (0, [], 1)


def test_missing_name_and_context_are_none_or_empty_never_zero():
    claude = {"a": _d(base.STATE_RUNNING, name=""),
              "b": _d(base.STATE_RUNNING, name=None)}
    rows = session_rows.build_rows(claude, {}, {})
    assert all(r.ctx is None for r in rows)
    assert all(r.name == "" for r in rows)


def test_context_is_per_session_and_claude_only():
    claude = {"a": _d(base.STATE_RUNNING, name="A", age=1),
              "b": _d(base.STATE_RUNNING, name="B", age=2)}
    codex = {"a": _d(base.STATE_RUNNING, name="X", age=3)}
    rows = session_rows.build_rows(claude, codex, {"a": 30, "b": 70})
    by = {(r.provider, r.name): r.ctx for r in rows}
    assert by == {("c", "A"): 30, ("c", "B"): 70, ("x", "X"): None}


def test_a_bad_context_value_is_unknown():
    claude = {"a": _d(base.STATE_RUNNING)}
    for bad in (True, 150, -1, "40", 4.5):
        rows = session_rows.build_rows(claude, {}, {"a": bad})
        assert rows[0].ctx is None


def test_ages_are_whole_and_never_negative():
    claude = {"a": _d(base.STATE_IDLE, age=-5.0),
              "b": _d(base.STATE_IDLE, age=float("nan")),
              "c": _d(base.STATE_IDLE, age=61.9)}
    ages = sorted(r.age_s for r in session_rows.build_rows(claude, {}, {}))
    assert ages == [0, 0, 61]


def test_malformed_details_are_skipped():
    rows = session_rows.build_rows({"a": ("running",), "b": None,
                                    "c": _d(base.STATE_IDLE)}, None, None)
    assert len(rows) == 1
