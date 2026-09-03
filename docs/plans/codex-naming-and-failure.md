# Codex Naming and Failure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a Codex session the two things a Claude session already has on the panel — a project name under the status line, and a red `failed` state when a turn dies — using only the rollout files Codex already writes.

**Architecture:** `pc/providers/codex_cli.py` reads the last 256 KB of each recent rollout. The project name is on line 1 of the file and the biggest rollout on this machine is 51 MB, so the name gets its own small read of the *front* of the file, cached per path because `session_meta` is written once and never rewritten. Failure comes from the same tail the state machine already scans: a `task_complete` that carries an `error` object is a turn that died, and `UsageLimitExceeded` is inside it. Before either can reach the board, `protocol.encode` is fixed to stop escaping non-ASCII — a real filesystem path can be non-ASCII, and today the firmware would draw the escape sequence.

**Tech Stack:** Python 3.10+, pytest, POSIX sh (the contract script). No new dependencies.

**Spec:** `/private/tmp/claude-502/-Users-KfirLevy-Projects-LiveClaudeUi/aeb3001d-3255-41ed-b053-ecf8e0cdec4c/scratchpad/codex-research.md` — the research that establishes what the rollout file can and cannot answer. Sections Q1 (naming) and Q3 (failure) are what this plan implements. Q2 (`waiting`) is explicitly out of scope and belongs to the Codex-hooks plan that follows this one.

## Global Constraints

- **No wire change.** No new message type, no new field on `usage`, no new value in `base.VALID_STATES`. The `usage` message measures **511** of `protocol.MAX_LINE_BYTES = 512` fully loaded, and `firmware/src/proto.c` drops an over-long line **whole** — a silent panel freeze. That 511 was re-measured in Task 0 through `test_the_widest_line_the_daemon_can_build_still_fits`, the only measurement that counts because it is the one built through `frame_to_usage`; the 506 this plan was written against predates the widest case that guard now covers, so the real headroom is **one byte**, not six. The `session` message is the one that carries the label, and it measures **66 bytes** fully loaded (`{"t":"session","v":2,"n":9999,"label":"<24 bytes>"}` + newline), so there is room there and nowhere else.
- **`protocol.SESSION_LABEL_MAX_BYTES = 24`**, truncated on a UTF-8 boundary by `protocol.session()`. That truncation is already correct and already tested (`tests/pc/test_protocol.py::test_session_label_survives_multibyte_truncation`). Do not re-implement it in the provider.
- **`turn_aborted` stays `idle`.** All four `TurnAbortReason` values (`interrupted`, `replaced`, `review_ended`, `budget_limited`) are things the person did. The owner has frozen this mapping; do not touch it.
- **`codex exec` batch runs count as sessions and get named.** No originator filter. Decided by the owner.
- **Python 3.10+**, matching the existing style in `pc/providers/`.
- **Comments explain WHY, in prose, at the density of the surrounding file.** `codex_cli.py` and `claude_state.py` both carry long explanatory blocks above the constants they justify. Match that; a bare constant with no reason is a review failure here.
- **Parsers must not raise.** `base.ProviderParser`: "A provider whose source has changed shape upstream is expected to return None and let the bus fall back to another source." Every new code path degrades to "no name" or "idle", never to an exception.
- **`pytest tests -q` passes at every commit.** Baseline is **593** with `ecdsa` installed; **561** with `--ignore=tests/pc/test_update.py` if it is not. (Both measured in Task 0 on 2026-09-04; the 515/483 this plan was written against are from before the liveness and pid work landed on this branch, which is why Task 0 re-measures rather than trusting a written-down number.) Establish your own baseline in Task 0 and hold it plus the tests you add.
- **`sh tests/ci/check_codex_contract.sh` passes** at every commit from Task 8 onward. It needs network (it fetches from `raw.githubusercontent.com`) or `CODEX_SRC_DIR`.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `pc/protocol.py` | the wire encoder and every message shape | `encode()` stops escaping non-ASCII |
| `pc/providers/codex_cli.py` | the Codex rollout reader | head-read, name sanitising, per-path name cache, `label` on the state frame, `failed` from `task_complete.error` |
| `pc/ingest.py` | the bus; picks the label for the wire | **no change** — `_pair_from` already implements the precedence rule; Task 7 only pins it |
| `pc/normalizer.py` | field-by-field merge | **no change** — `label=state_src.label` already carries it |
| `firmware/` | the panel | **no change** — `fmt_hint()` already transliterates raw UTF-8 through `fmt_ascii()`, pinned by `tests/fmt/host_test.c` ("caf\xc3\xa9" → "Working - caf?") |
| `tests/pc/test_protocol.py` | wire-shape tests | the non-ASCII encoding tests |
| `tests/pc/test_codex_cli.py` | the reader's tests | head-read, naming, caching, failure |
| `tests/pc/test_ingest.py` | the bus's tests | the two-providers-both-named precedence tests |
| `tests/ci/check_codex_contract.sh` | watches upstream's field names | the new fields this reader now depends on |

Tasks are strictly ordered. Task 1 must land before Task 5, because Task 5 is what first lets a non-ASCII string reach the label on a real desk.

---

## The three judgement calls, and why

**1. The head-read is cached, per path, pruned to the current file set on every poll.**
Not for CPU: the daemon polls roughly once a minute (`pc/bridge.py:81`, "the first usage message waits for the 60 s poll") and six 19 KB `json.loads` calls per minute is under a millisecond. It is cached because the value is *immutable by construction* — `session_meta` is line 1 of an append-only file whose name carries a UUID, so a path is never reused and the answer for a path never changes — and because the **size** of that line belongs to Codex, not to us: `session_meta` embeds `base_instructions`, and the four real rollouts on this machine have first lines of **18–19 KB**, not the "few KB" a line of JSON sounds like. Re-deriving a fixed value from a blob upstream is free to grow is waste that grows with it. Pruning to `recent_rollouts()` bounds the dict at `RECENT_FILES` = 6 entries, so the cache cannot outlive the files it describes.

**2. Claude versus Codex: there is no precedence, and that is the rule.**
`pc/ingest._pair_from` already resolves this and resolves it correctly, so this plan changes no code for it and adds tests instead. The rule: the label survives only when exactly one frame is in the state the panel is actually showing (`worst_of(primary, secondary)`) *and* the summed count for that state is 1. Two named sessions in the same state produce no name and the count carries the meaning. Picking a winner — "Claude first", or "the fresher frame" — would put one project's name over a line that is equally true of another project, which is a wrong sentence rather than a vague one. That is the same refusal `claude_state.poll` already applies within one provider; Codex getting a name simply makes the cross-provider case reachable for the first time, which is why it needs a test and not a policy.

**3. `UsageLimitExceeded` gets no distinct treatment. Every turn-failing error is `failed`.**
Three reasons. (a) There is nowhere to put it: `base.VALID_STATES` is fixed and adding a state is a wire change the Global Constraints forbid. (b) The panel already says it, from the other half of the same file: `usage_view.c:2277` draws "Session used up" off the *percentage* dial, and the `rate_limits` that fills that dial is read by this same provider from this same rollout. A rate-limited Codex turn shows a full ring and "Session failed" together; the two lines already say what happened. (c) Branching panel copy on `codex_error_info` string values that have **never been observed in a real file** is exactly the confident-wrongness this codebase refuses elsewhere. What the reader *does* mirror is upstream's own `CodexErrorInfo::affects_turn_status` predicate, which is a two-name exclusion, not an enumeration of the failures — see Task 6.

---

### Task 0: Establish the baseline

**Files:** none — this task writes nothing.

**Interfaces:**
- Produces: the number every later task's "Expected: PASS" is measured against.

- [ ] **Step 1: Run the suite and record the count**

Run: `pytest tests -q`

If it stops with `ModuleNotFoundError: No module named 'ecdsa'`, either `pip install ecdsa` or run `pytest tests -q --ignore=tests/pc/test_update.py` and use that number instead. Expected: `593 passed` (or `561 passed` with `test_update.py` ignored). Write the number down; every later task adds to it and none subtracts.

- [ ] **Step 2: Run the contract script**

Run: `sh tests/ci/check_codex_contract.sh`
Expected: `PASS [codex contract at main]`. If it fails here, before any change, stop and report — Task 8 extends this script and cannot be judged against a red baseline.

---

### Task 1: The wire stops escaping non-ASCII

**Files:**
- Modify: `pc/protocol.py:39-41` (`encode`)
- Test: `tests/pc/test_protocol.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `protocol.encode(msg) -> bytes` — unchanged signature, changed bytes. Task 5 depends on this being done first.

**Why this is in this plan at all.** `encode` calls `json.dumps` with `ensure_ascii` at its default, so `"café-project"` goes on the wire as `caf\u00e9-project`. `firmware/src/msg_parse.c:47 msg_get_str()` copies the bytes between two quotes and does **no unescaping at all**, so the panel draws the literal characters `\u00e9`. Measured on this branch:

```
protocol.encode(protocol.session("café-project-name-abcdef", 9999))
b'{"t":"session","v":2,"n":9999,"label":"caf\\u00e9-project-name-abcde"}\n'
```

— 70 bytes for a label that is 24 bytes of UTF-8, and `char lbl[28]` in `proto.c:632` then truncates the escape sequence too.

The alternative was to refuse non-ASCII at the Codex boundary and leave the bug. That is rejected: the firmware **already** ships a UTF-8 decoder for exactly this (`fmt_ascii` in `firmware/src/fmt.c:90`, with `tests/fmt/host_test.c` pinning `"caf\xc3\xa9"` → `"Working - caf?"`), and the daemon's escaping is the only reason it is unreachable. Refusing at the boundary would leave a one-line live defect armed for the next source that carries a non-ASCII string. Fixing it here is also strictly safe on the byte budget: UTF-8 is never longer than `\uXXXX`, so every line this change touches gets **shorter or stays the same** and no message can newly breach `MAX_LINE_BYTES`.

- [ ] **Step 1: Write the failing test**

Add to `tests/pc/test_protocol.py`, inside the same `unittest.TestCase` class that holds `test_session_caps_the_label` (search for that method and put these beside it):

```python
    def test_encode_puts_utf8_on_the_wire_not_escapes(self):
        """The firmware does no unescaping, so an escape is drawn literally.

        msg_parse.c copies the bytes between two quotes and hands them to
        fmt_ascii(), which decodes UTF-8 and transliterates what it cannot
        draw. json.dumps' default ensure_ascii=True defeated that entirely:
        "café" arrived as the six characters \\u00e9 and the panel drew them.
        """
        raw = protocol.encode(protocol.session("café", 1))
        self.assertIn("café".encode("utf-8"), raw)
        self.assertNotIn(b"\\u00e9", raw)

    def test_encode_of_non_ascii_is_never_longer_than_the_escaped_form(self):
        """The byte budget can only improve. proto.c drops an over-long line
        whole, so a change to the encoder has to be shown not to lengthen
        anything before it can be believed."""
        import json as _json
        msg = protocol.session("café-project-name-abcdef", 9999)
        escaped = (_json.dumps(msg, separators=(",", ":"),
                               ensure_ascii=True) + "\n").encode("utf-8")
        self.assertLessEqual(len(protocol.encode(msg)), len(escaped))
        self.assertLessEqual(len(protocol.encode(msg)),
                             protocol.MAX_LINE_BYTES)

    def test_a_non_ascii_label_still_fits_the_line_limit(self):
        """The worst case for the label field: 24 bytes of four-byte
        codepoints, which ensure_ascii would have turned into 12 escapes."""
        msg = protocol.session("𝔅" * 12, 9999)
        raw, reason = protocol.encode_checked(msg)
        self.assertIsNone(reason)
        self.assertLessEqual(len(raw), protocol.MAX_LINE_BYTES)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/pc/test_protocol.py -q -k "utf8_on_the_wire or never_longer or non_ascii_label"`
Expected: `test_encode_puts_utf8_on_the_wire_not_escapes` FAILS on the `assertIn` (the raw UTF-8 bytes are not in the line). The other two pass already — they are the guards that the fix does not cost anything, and a guard that passes before and after is doing its job.

- [ ] **Step 3: Write the implementation**

Replace `pc/protocol.py:39-41` with:

```python
def encode(msg: dict) -> bytes:
    """Serialize a message dict to a single NDJSON line (bytes).

    ensure_ascii=False, and that is a firmware decision rather than a taste
    one. json.dumps escapes non-ASCII by default, msg_parse.c copies the
    bytes between two quotes and unescapes nothing, and fmt.c then draws
    whatever it was given -- so a project called "café" reached the panel as
    the literal characters \\u00e9 and was drawn that way. The firmware has
    had a UTF-8 decoder for this since fmt_ascii() was written
    (tests/fmt/host_test.c pins "caf\\xc3\\xa9" -> "caf?"); the escaping here
    was the only thing keeping it out of reach.

    It cannot cost line budget either, which matters because proto.c drops an
    over-long line whole: a UTF-8 sequence is at most four bytes and the
    escape it replaces is at least six, so every line this touches gets
    shorter or stays the same.
    """
    return (json.dumps(msg, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_protocol.py -q`
Expected: PASS, no failures.

- [ ] **Step 5: Run the whole suite**

Run: `pytest tests -q`
Expected: baseline + 3 passed.

- [ ] **Step 6: Commit**

```bash
git add pc/protocol.py tests/pc/test_protocol.py
git commit -m "fix: a non-ASCII project name reached the panel as its own escape sequence"
```

---

### Task 2: Read the first line of a rollout

**Files:**
- Modify: `pc/providers/codex_cli.py` (add after `_tail_lines`, around line 132)
- Test: `tests/pc/test_codex_cli.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `codex_cli.HEAD_BYTES` (int) and `codex_cli._head_line(path: str) -> str` — Task 4 calls it.

- [ ] **Step 1: Write the failing test**

Add to `tests/pc/test_codex_cli.py`, after `test_only_the_tail_of_a_huge_file_is_read` (search for that name):

```python
# --- the head of the file: where the project name lives ----------------------


def test_the_first_line_is_read_whatever_follows_it(tmp_path):
    """The name is on line 1 and the tail read cannot reach it: the biggest
    rollout on the machine this was written against is 51 MB. So the head
    gets its own read, and it must not care how much comes after."""
    path = str(tmp_path / "rollout-a.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type":"session_meta","payload":{"cwd":"/a/b"}}\n')
        f.write("x" * (codex_cli.HEAD_BYTES * 3) + "\n")
    assert codex_cli._head_line(path) == \
        '{"type":"session_meta","payload":{"cwd":"/a/b"}}'


def test_a_first_line_longer_than_the_head_bound_is_refused_not_halved(tmp_path):
    """A JSON object cut in half parses as nothing anyway, and returning the
    fragment would only move the failure into json.loads. The cost of a first
    line that does not fit is the name, not the read."""
    path = str(tmp_path / "rollout-b.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type":"session_meta","payload":{"pad":"'
                + "p" * (codex_cli.HEAD_BYTES + 10) + '"}}\n')
    assert codex_cli._head_line(path) == ""


def test_the_head_bound_clears_a_real_session_meta_line():
    """18-19 KB is what the four real rollouts on this machine measure, and
    that length is upstream's to change -- base_instructions is embedded in
    the record. The bound has to have room over the observation, not equal
    it."""
    assert codex_cli.HEAD_BYTES >= 4 * 19 * 1024


def test_a_missing_file_is_silence_not_an_error(tmp_path):
    assert codex_cli._head_line(str(tmp_path / "nope.jsonl")) == ""


def test_a_file_with_no_newline_at_all_is_refused(tmp_path):
    """A rollout being written right now can have a partial first line. It
    is not a name yet, and it will be on the next poll."""
    path = str(tmp_path / "rollout-c.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type":"session_meta","payload":{"cwd":"/a')
    assert codex_cli._head_line(path) == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/pc/test_codex_cli.py -q -k "first_line or head_bound or no_newline or missing_file"`
Expected: FAIL with `AttributeError: module 'pc.providers.codex_cli' has no attribute 'HEAD_BYTES'`.

- [ ] **Step 3: Write the implementation**

In `pc/providers/codex_cli.py`, insert immediately after `_tail_lines` (which ends at line 131 with `return text.splitlines()`):

```python
# The other end of the same file. `session_meta` -- the record that carries
# the project's cwd, and so the only name a Codex session can ever have -- is
# line 1, and the tail read above will never see it: the biggest rollout on
# the machine this was written against is 51 MB, so TAIL_BYTES would have to
# grow to the size of the file to reach the front of it. The name therefore
# gets its own small read, of the head, and the two never meet.
#
# 64 KB rather than the "a few KB" a line of JSON sounds like. `session_meta`
# embeds `base_instructions`, and the four real rollouts here have first
# lines of 18-19 KB. That number is Codex's to change, so the bound is
# deliberately several times the observation -- and a first line that still
# does not fit costs the name, not the read.
HEAD_BYTES = 64 * 1024


def _head_line(path: str) -> str:
    """The first COMPLETE line of a file, or "".

    Complete is the whole point. A line with no newline inside HEAD_BYTES is
    refused rather than returned in part: a JSON object cut in half decodes
    as nothing, and handing the fragment back would only move the failure
    into json.loads. A rollout being written at this instant is the ordinary
    case for that, and it will have its newline by the next poll.
    """
    try:
        with open(path, "rb") as f:
            blob = f.read(HEAD_BYTES)
    except OSError:
        return ""
    nl = blob.find(b"\n")
    if nl < 0:
        return ""
    return blob[:nl].decode("utf-8", "replace")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_codex_cli.py -q`
Expected: PASS, no failures.

- [ ] **Step 5: Commit**

```bash
git add pc/providers/codex_cli.py tests/pc/test_codex_cli.py
git commit -m "feat: read the head of a rollout, where the session's cwd is"
```

---

### Task 3: A cwd becomes a project name, or nothing

**Files:**
- Modify: `pc/providers/codex_cli.py` (add after `_head_line`)
- Test: `tests/pc/test_codex_cli.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (these two functions are pure).
- Produces:
  - `codex_cli.session_meta_cwd(head_line: str) -> str | None`
  - `codex_cli._project_name(cwd) -> str`

  Task 4 composes them as `_project_name(session_meta_cwd(_head_line(path)))`.

- [ ] **Step 1: Write the failing test**

Add to `tests/pc/test_codex_cli.py`, after the head-read tests from Task 2:

```python
def test_the_cwd_comes_out_of_a_session_meta_line():
    line = json.dumps({"type": "session_meta", "timestamp": "2026-08-27",
                       "payload": {"cwd": "/Users/K/Projects/LiveClaudeUi",
                                   "originator": "codex-tui"}})
    assert codex_cli.session_meta_cwd(line) == "/Users/K/Projects/LiveClaudeUi"


def test_a_line_that_is_not_session_meta_yields_no_cwd():
    """None rather than "" so the caller can tell a head it could not read
    from a directory it read and then refused."""
    assert codex_cli.session_meta_cwd(
        json.dumps({"type": "event_msg", "payload": {"cwd": "/a/b"}})) is None
    assert codex_cli.session_meta_cwd("") is None
    assert codex_cli.session_meta_cwd("session_meta but not json") is None
    assert codex_cli.session_meta_cwd(json.dumps(["session_meta"])) is None
    assert codex_cli.session_meta_cwd(
        json.dumps({"type": "session_meta", "payload": "session_meta"})) is None
    assert codex_cli.session_meta_cwd(
        json.dumps({"type": "session_meta", "payload": {}})) is None
    assert codex_cli.session_meta_cwd(
        json.dumps({"type": "session_meta", "payload": {"cwd": 7}})) is None


def test_the_name_is_the_last_path_component():
    assert codex_cli._project_name("/Users/K/Projects/LiveClaudeUi") == \
        "LiveClaudeUi"
    assert codex_cli._project_name("/private/tmp") == "tmp"


def test_a_trailing_separator_is_not_a_component():
    assert codex_cli._project_name("/Users/K/Blink/") == "Blink"
    assert codex_cli._project_name("/Users/K/Blink///") == "Blink"


def test_a_windows_path_splits_on_the_windows_separator():
    """Both separators, not os.sep: a home directory can be synced between
    machines, and Codex on Windows writes C:\\Users\\...."""
    assert codex_cli._project_name("C:\\Users\\Kfir\\Projects\\Blink") == "Blink"
    assert codex_cli._project_name("C:\\Users\\Kfir\\Projects\\Blink\\") == "Blink"


def test_a_directory_entry_is_not_a_name():
    for cwd in ("/", "", ".", "..", "/a/b/.", "/a/b/.."):
        assert codex_cli._project_name(cwd) == "", cwd


def test_a_control_character_never_reaches_the_wire():
    """This string is JSON-encoded into a line the firmware scans for quotes.
    A newline in a directory name is legal on every platform this runs on."""
    assert codex_cli._project_name("/a/b/pro\nject") == "project"
    assert codex_cli._project_name("/a/b/pro\x7fject") == "project"
    assert codex_cli._project_name("/a/b/\n\n") == ""


def test_a_name_with_no_drawable_ascii_is_refused():
    """firmware/src/fmt.c draws a label through fmt_ascii(), which replaces
    every codepoint it has no ASCII spelling for with "?" -- pinned by
    tests/fmt/host_test.c. A wholly non-Latin name therefore arrives as a row
    of question marks, which is worse than the count the panel falls back to.
    A name with a Latin stem keeps it and loses the rest.
    """
    assert codex_cli._project_name("/Users/K/פרויקט") == ""
    assert codex_cli._project_name("/Users/K/项目") == ""
    assert codex_cli._project_name("/Users/K/café") == "café"
    assert codex_cli._project_name("/Users/K/proj-项目") == "proj-项目"


def test_a_name_with_spaces_is_kept():
    """Unlike the Claude hook shim, which is one sed and refuses them. There
    is no filename being built here and no shell quoting to get wrong, so the
    reason that rule exists there does not exist here."""
    assert codex_cli._project_name("/Users/K/My Project") == "My Project"


def test_a_cwd_that_is_not_a_string_is_refused_not_raised():
    for cwd in (None, 7, [], {}, True):
        assert codex_cli._project_name(cwd) == ""


def test_the_name_is_not_capped_here():
    """protocol.session is the one place that knows the byte bound and the
    one place that truncates on a UTF-8 boundary. A second cap here would be
    a second thing to keep in step with the firmware."""
    long = "n" * 200
    assert codex_cli._project_name("/a/" + long) == long
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/pc/test_codex_cli.py -q -k "cwd or project_name or last_path or trailing_separator or windows_path or directory_entry or control_character or drawable_ascii or with_spaces or capped_here"`
Expected: FAIL with `AttributeError: module 'pc.providers.codex_cli' has no attribute 'session_meta_cwd'`.

- [ ] **Step 3: Write the implementation**

In `pc/providers/codex_cli.py`, insert immediately after `_head_line`:

```python
def session_meta_cwd(head_line: str):
    """The `cwd` out of a rollout's first line, or None.

    None rather than "": the caller has two different failures to tell apart
    -- a head it could not read, and a directory it read and then refused --
    and only one of them is worth ever looking at again.
    """
    if "session_meta" not in head_line:
        return None         # cheap reject before parsing 19 KB of JSON
    try:
        line = json.loads(head_line)
    except ValueError:
        return None
    if not isinstance(line, dict) or line.get("type") != "session_meta":
        return None
    payload = line.get("payload")
    if not isinstance(payload, dict):
        return None
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) else None


# Both, not os.sep. This file may be read on one platform and written on
# another -- a synced home directory is ordinary -- and Codex on Windows
# writes C:\Users\....
_NAME_SEPARATORS = "/\\"


def _project_name(cwd) -> str:
    """The project name a Codex `cwd` implies, or "".

    The last path component, with three refusals. The first two are the ones
    the Claude hook shim already applies to its own cwd: `.` and `..` are
    directory entries rather than names, and control characters are stripped
    because this string is JSON-encoded into a line the firmware scans for
    quotes.

    The third is about the panel rather than about safety, and it is the one
    the shim has no need of. firmware/src/fmt.c draws a label through
    fmt_ascii(), which replaces every codepoint it has no ASCII spelling for
    with "?" -- tests/fmt/host_test.c pins "caf\xc3\xa9" arriving as "caf?".
    A wholly non-Latin name therefore reaches the desk as a row of question
    marks, which says less than the count the panel falls back to when there
    is no name at all. So a name has to carry at least one ASCII letter or
    digit to be worth sending: "café" keeps its name and loses one letter,
    "פרויקט" is refused and the count speaks instead.

    Not capped here. protocol.session is the one place that knows the byte
    bound and the one place that truncates on a UTF-8 boundary; a second cap
    would be a second thing to keep in step with the firmware.
    """
    if not isinstance(cwd, str):
        return ""
    name = cwd
    while name and name[-1] in _NAME_SEPARATORS:
        name = name[:-1]
    for sep in _NAME_SEPARATORS:
        name = name.rsplit(sep, 1)[-1]
    name = "".join(c for c in name if c >= " " and c != "\x7f")
    if name in ("", ".", ".."):
        return ""
    if not any(("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9")
               for c in name):
        return ""
    return name
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_codex_cli.py -q`
Expected: PASS, no failures.

- [ ] **Step 5: Commit**

```bash
git add pc/providers/codex_cli.py tests/pc/test_codex_cli.py
git commit -m "feat: turn a Codex cwd into a name the panel can draw"
```

---

### Task 4: The name is read once per file

**Files:**
- Modify: `pc/providers/codex_cli.py` (`CodexCliProvider.__init__` at line 304, plus a new method)
- Test: `tests/pc/test_codex_cli.py`

**Interfaces:**
- Consumes: `_head_line` (Task 2), `session_meta_cwd` and `_project_name` (Task 3).
- Produces: `CodexCliProvider._name_for(path: str) -> str` and `CodexCliProvider._prune_names(known)` — Task 5 calls both.

- [ ] **Step 1: Write the failing test**

Add to `tests/pc/test_codex_cli.py`, after the naming tests from Task 3:

```python
def test_the_name_is_read_once_per_file(tmp_path):
    """`session_meta` is line 1 of an append-only file whose name carries a
    UUID: the path is never reused and the answer never changes. Re-deriving
    it from 19 KB of embedded system prompt on every tick is waste that grows
    with whatever upstream puts in that record next."""
    root = str(tmp_path / "sessions")
    path = write_rollout(root, lines=[meta_line("/Users/K/Blink"),
                                      token_count_line(rate_limits())])
    p = codex_cli.CodexCliProvider(root=root)
    reads = []
    real = codex_cli._head_line
    codex_cli._head_line = lambda q: (reads.append(q), real(q))[1]
    try:
        assert p._name_for(path) == "Blink"
        assert p._name_for(path) == "Blink"
        assert p._name_for(path) == "Blink"
    finally:
        codex_cli._head_line = real
    assert reads == [path], reads


def test_a_file_with_no_usable_name_is_not_re_read_either(tmp_path):
    """The negative answer is as fixed as the positive one, and a rollout
    with no session_meta is the common case for a file being written right
    now -- exactly the file this would otherwise re-read every minute."""
    root = str(tmp_path / "sessions")
    path = write_rollout(root, lines=[token_count_line(rate_limits())])
    p = codex_cli.CodexCliProvider(root=root)
    reads = []
    real = codex_cli._head_line
    codex_cli._head_line = lambda q: (reads.append(q), real(q))[1]
    try:
        assert p._name_for(path) == ""
        assert p._name_for(path) == ""
    finally:
        codex_cli._head_line = real
    assert reads == [path], reads


def test_names_are_pruned_to_the_files_still_being_read(tmp_path):
    """Bounded by RECENT_FILES rather than by how long the daemon has been
    up. A path that has fallen out of the recent set will never be read
    again, so holding its name is holding a string for nothing."""
    p = codex_cli.CodexCliProvider(root=str(tmp_path))
    p._names = {"/a": "A", "/b": "B", "/c": "C"}
    p._prune_names({"/b", "/c", "/d"})
    assert p._names == {"/b": "B", "/c": "C"}


def test_two_providers_do_not_share_a_name_cache(tmp_path):
    """A mutable default on __init__ would make the cache class-wide, which
    is a bug that only shows up on a desk running two of these."""
    a = codex_cli.CodexCliProvider(root=str(tmp_path))
    b = codex_cli.CodexCliProvider(root=str(tmp_path))
    a._names["/a"] = "A"
    assert b._names == {}
```

Add this helper beside `token_count_line` near the top of the file (after `write_rollout`):

```python
def meta_line(cwd, originator="codex-tui"):
    """Line 1 of a real rollout: the record that carries the project's cwd.

    The padding is not decoration. A real session_meta embeds
    base_instructions and measures 18-19 KB, and a fixture of 80 bytes would
    let a head bound far too small to work on a desk pass every test here.
    """
    return json.dumps({
        "timestamp": "2026-08-27T03:00:00.000Z",
        "type": "session_meta",
        "payload": {"cwd": cwd, "originator": originator,
                    "cli_version": "0.150.0",
                    "base_instructions": "i" * 18_000},
    })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/pc/test_codex_cli.py -q -k "once_per_file or not_re_read or pruned or share_a_name_cache"`
Expected: FAIL with `AttributeError: 'CodexCliProvider' object has no attribute '_name_for'`.

- [ ] **Step 3: Write the implementation**

Replace `CodexCliProvider.__init__` (currently `pc/providers/codex_cli.py:304-305`) and add `_name_for`/`_prune_names` after `root()`:

```python
    def __init__(self, root=None):
        self._root = root
        # path -> project name, for the one record in a rollout that cannot
        # change. `session_meta` is line 1 of an append-only file whose name
        # carries a UUID, so a path is never reused and the answer for a path
        # is fixed for the life of the file.
        #
        # The saving is not CPU. The daemon polls about once a minute and
        # 19 KB of json.loads is under a millisecond. It is that the SIZE of
        # that line belongs to Codex: base_instructions is embedded in it,
        # and re-deriving an immutable value from a blob upstream is free to
        # grow is waste that grows with it. Pruned to the current file set on
        # every poll, so this is bounded by RECENT_FILES rather than by how
        # long the daemon has been up.
        self._names = {}
```

```python
    def _name_for(self, path):
        """The project name for one rollout, read once per file.

        The negative answer is cached too, and deliberately: a rollout with
        no readable session_meta yet is a file being written at this instant,
        which is precisely the file that would otherwise be re-read on every
        poll for as long as it stayed in the recent set.
        """
        if path not in self._names:
            self._names[path] = _project_name(
                session_meta_cwd(_head_line(path)))
        return self._names[path]

    def _prune_names(self, known):
        """Forget every cached name whose file is no longer being read."""
        self._names = {p: n for p, n in self._names.items() if p in known}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_codex_cli.py -q`
Expected: PASS, no failures.

- [ ] **Step 5: Commit**

```bash
git add pc/providers/codex_cli.py tests/pc/test_codex_cli.py
git commit -m "feat: cache a rollout's project name, which cannot change"
```

---

### Task 5: The state frame carries the name

**Files:**
- Modify: `pc/providers/codex_cli.py` (`CodexCliProvider.poll`, lines 335-379)
- Test: `tests/pc/test_codex_cli.py`

**Interfaces:**
- Consumes: `_name_for` and `_prune_names` (Task 4); `base.NormalizedUsageFrame.label` (`pc/providers/base.py:135`).
- Produces: a `label` on the `STATE_SRC_ID` frame. `pc/normalizer.merge` already carries it (`normalizer.py:192`) and `pc/ingest._pair_from` already consumes it; neither changes.

- [ ] **Step 1: Write the failing test**

Add to `tests/pc/test_codex_cli.py`, after `test_no_state_frame_when_no_rollout_has_a_turn`:

```python
# --- naming the session, when there is exactly one to name -------------------


def _state_frame(root, now=NOW):
    frames = codex_cli.CodexCliProvider(root=root).poll(now)
    held = [f for f in frames if f.src == codex_cli.STATE_SRC_ID]
    return held[0] if held else None


def test_the_one_session_in_the_winning_state_is_named(tmp_path):
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Projects/LiveClaudeUi"),
                         token_count_line(rate_limits()),
                         turn_line("task_complete", _stamp(NOW - 5))])
    st = _state_frame(root)
    assert (st.state, st.label) == ("idle", "LiveClaudeUi")


def test_two_sessions_in_the_winning_state_are_not_named(tmp_path):
    """The rule claude_state.poll applies, applied here for the same reason:
    a name picked from two says something true about one and implies it about
    the other. The count is what is true of both."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Blink"),
                         token_count_line(rate_limits()),
                         turn_line("task_complete", _stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[meta_line("/Users/K/Other"),
                         turn_line("task_complete", _stamp(NOW - 9))])
    st = _state_frame(root)
    assert (st.state, st.n_idle, st.label) == ("idle", 2, "")


def test_a_session_in_a_lesser_state_does_not_lend_its_name(tmp_path):
    """Two sessions, two states. The frame's `state` is the worse of them, so
    only the session actually holding that state may be named -- the other's
    name under the other's status would be a wrong sentence."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Finished"),
                         token_count_line(rate_limits()),
                         turn_line("task_complete", _stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[meta_line("/Users/K/Working"),
                         turn_line("task_started", _stamp(NOW - 9))])
    st = _state_frame(root)
    assert (st.state, st.label) == ("idle", "Finished")


def test_an_unnamed_session_leaves_a_named_one_alone(tmp_path):
    """A rollout whose session_meta could not be read is still a session and
    still votes on the state -- it just has nothing to add to the name. Two
    holders of the state is still two, named or not."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Blink"),
                         token_count_line(rate_limits()),
                         turn_line("task_complete", _stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[turn_line("task_complete", _stamp(NOW - 9))])
    st = _state_frame(root)
    assert (st.state, st.n_idle, st.label) == ("idle", 2, "")


def test_a_rollout_with_no_turn_yet_lends_no_name(tmp_path):
    """It makes no claim on the state, so it must make none on the name --
    otherwise an opened-and-untyped-into terminal would rename the panel."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Blink"),
                         token_count_line(rate_limits()),
                         turn_line("task_started", _stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[meta_line("/Users/K/JustOpened")])
    st = _state_frame(root)
    assert (st.state, st.n_run, st.label) == ("running", 1, "Blink")


def test_a_poll_prunes_the_names_of_files_it_no_longer_reads(tmp_path):
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Blink"),
                         token_count_line(rate_limits()),
                         turn_line("task_started", _stamp(NOW - 5))])
    p = codex_cli.CodexCliProvider(root=root)
    p._names["/gone/rollout-z.jsonl"] = "Ghost"
    p.poll(NOW)
    assert "/gone/rollout-z.jsonl" not in p._names
    assert list(p._names.values()) == ["Blink"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/pc/test_codex_cli.py -q -k "winning_state or lesser_state or unnamed_session or no_turn_yet_lends or prunes_the_names"`
Expected: FAIL — `assert ('idle', '') == ('idle', 'LiveClaudeUi')`; the frame has no label.

- [ ] **Step 3: Write the implementation**

Replace the body of `CodexCliProvider.poll` (`pc/providers/codex_cli.py:335-379`) with:

```python
    def poll(self, now_epoch):
        """The freshest reading Codex has written, as a one-frame list.

        One frame, not one per rollout file: the two percentages are
        account-wide, so several open terminals all describe the same pair of
        windows and handing the normalizer six copies of it would only make
        the freshest one win a contest it has already won here.
        """
        best = None
        counts = {}
        names = {}
        paths = recent_rollouts(self._root)
        self._prune_names(set(paths))
        for path in paths:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            lines = _tail_lines(path)
            # Every rollout is one session, so every one of them votes on
            # the execution state -- unlike the percentages, which are one
            # account-wide pair however many terminals are open.
            state = parse_rollout_state(lines, now_epoch)
            if state != base.STATE_UNKNOWN:
                counts[state] = counts.get(state, 0) + 1
                # A name is only collected from a session that made a claim.
                # A terminal opened and not typed into has a cwd and no turn,
                # and letting it lend its name would rename the panel after
                # the session that is actually doing something.
                name = self._name_for(path)
                if name:
                    names.setdefault(state, []).append(name)
            limits, observed_at = parse_rollout_tail(lines, mtime)
            if limits is None:
                continue
            frame = self.parse_cli_event(limits, now_epoch, observed_at)
            if frame is None:
                continue
            if best is None or frame.observed_at > best.observed_at:
                best = frame
        frames = [best] if best is not None else []
        if counts:
            # A separate frame with no percentages, exactly as Claude's state
            # provider does it: it can never win a recency contest for
            # numbers, and the normalizer merges its state field by field.
            state = base.worst_of(counts)
            held = names.get(state, [])
            frames.append(base.NormalizedUsageFrame(
                provider=PROVIDER_ID,
                src=STATE_SRC_ID,
                observed_at=now_epoch,
                state=state,
                # Named only when exactly ONE session holds the state the
                # frame is reporting, which is the rule claude_state.poll
                # applies and for the same reason: a count says something
                # true about all of them, and a name picked from three says
                # something true about one and implies it about the rest.
                label=(held[0] if counts.get(state, 0) == 1 and len(held) == 1
                       else ""),
                n_run=counts.get(base.STATE_RUNNING, 0),
                n_idle=counts.get(base.STATE_IDLE, 0),
                n_stuck=counts.get(base.STATE_STUCK, 0),
            ))
        return frames
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_codex_cli.py -q`
Expected: PASS, no failures.

- [ ] **Step 5: Run the whole suite**

Run: `pytest tests -q`
Expected: baseline + 3 (Task 1) + 5 (Task 2) + 11 (Task 3) + 4 (Task 4) + 6 (Task 5) passed.

- [ ] **Step 6: Commit**

```bash
git add pc/providers/codex_cli.py tests/pc/test_codex_cli.py
git commit -m "feat: name the Codex session on the panel, when there is one to name"
```

---

### Task 6: A turn that died is `failed`, not `idle`

**Files:**
- Modify: `pc/providers/codex_cli.py` (the `_TURN_EVENTS` block at 258-266, `parse_rollout_state` at 269-300, and `n_stuck` in `poll`)
- Test: `tests/pc/test_codex_cli.py`

**Interfaces:**
- Consumes: `base.STATE_FAILED` (`pc/providers/base.py:37`), which `base.SEVERITY` already ranks above every other state. Nothing downstream needs teaching: `firmware/src/usage_state.c:22` already maps the wire string `"failed"` to `USAGE_ACTIVITY_FAILED`, which `usage_view.c:2214` draws as "Session failed". This is why `failed` costs no wire change — Claude has been sending it since `StopFailure` existed.
- Produces: `codex_cli._is_turn_failure(error) -> bool` and `codex_cli._NOT_A_TURN_FAILURE` — nothing later consumes them; Task 8 pins the upstream arm they mirror.

**What is being read.** `TurnCompleteEvent.error: Option<ErrorEvent>` — upstream's own comment is "Terminal error details when the turn completed unsuccessfully" — with `#[serde(skip_serializing_if = "Option::is_none")]`, so a successful turn has no `error` key at all. `ErrorEvent` serialises as `{"message": ..., "codex_error_info": ...}`; `codex_error_info` is a `CodexErrorInfo` whose unit variants (`usage_limit_exceeded`, `unauthorized`, …) serialise as bare snake_case strings and whose struct variants (`http_connection_failed`, `active_turn_not_steerable`) serialise as a single-key object. **Never observed in a real file** — every rollout on this machine is a success — so this branch is pinned to the upstream schema and to `check_codex_contract.sh`, and is written to degrade toward `idle` on any shape it does not recognise.

- [ ] **Step 1: Write the failing test**

Add to `tests/pc/test_codex_cli.py`, after the naming tests from Task 5:

```python
# --- a turn that died -------------------------------------------------------


def failed_line(stamp, info="usage_limit_exceeded", message="You have hit"):
    """A `task_complete` that carries an error, shaped as upstream writes it.

    ErrorEvent is {message, codex_error_info}; codex_error_info is a
    CodexErrorInfo, whose unit variants are bare snake_case strings.
    """
    error = {"message": message}
    if info is not None:
        error["codex_error_info"] = info
    return json.dumps({"timestamp": stamp, "type": "event_msg",
                       "payload": {"type": "task_complete",
                                   "turn_id": "t1", "error": error}})


def test_a_turn_that_died_on_a_usage_limit_is_failed():
    """The single case this product exists to warn about."""
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - 5))], NOW) == base.STATE_FAILED


def test_every_terminal_error_is_failed_not_just_the_limit_one():
    """No branch on which error. There is nowhere on the wire to put the
    distinction -- base.VALID_STATES is fixed -- and the panel already draws
    "Session used up" off the percentage dial this same file feeds."""
    for info in ("context_window_exceeded", "unauthorized",
                 "internal_server_error", "sandbox_error", "other"):
        assert codex_cli.parse_rollout_state(
            [failed_line(_stamp(NOW - 5), info=info)], NOW) \
            == base.STATE_FAILED, info


def test_a_struct_shaped_error_is_failed():
    """CodexErrorInfo::HttpConnectionFailed carries a field, so it
    serialises as a single-key object rather than a bare string."""
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - 5),
                     info={"http_connection_failed": {"http_status_code": 503}})],
        NOW) == base.STATE_FAILED


def test_an_error_upstream_does_not_call_a_turn_failure_is_still_idle():
    """CodexErrorInfo::affects_turn_status returns false for exactly two
    variants, both of them failures of a client operation rather than of the
    turn. Painting the panel red for a failed thread rollback would cry wolf
    with the one colour that must not."""
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - 5), info="thread_rollback_failed")],
        NOW) == base.STATE_IDLE
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - 5),
                     info={"active_turn_not_steerable": {"turn_kind": "review"}})],
        NOW) == base.STATE_IDLE


def test_an_error_with_no_info_is_failed():
    """ErrorEvent::affects_turn_status is is_none_or(...) -- upstream's own
    answer for a missing CodexErrorInfo is that the turn failed. An error
    object with nothing legible in it is still an error object."""
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - 5), info=None)], NOW) == base.STATE_FAILED
    for info in (None, 7, [], ["thread_rollback_failed"],
                 {"a": 1, "b": 2}, {}):
        line = json.dumps({"timestamp": _stamp(NOW - 5), "type": "event_msg",
                           "payload": {"type": "task_complete",
                                       "error": {"message": "boom",
                                                 "codex_error_info": info}}})
        assert codex_cli.parse_rollout_state([line], NOW) \
            == base.STATE_FAILED, info


def test_an_error_of_an_unexpected_shape_degrades_to_idle_not_to_red():
    """Never observed in a real file, so the shape is upstream's to change.
    Red is the loudest thing this panel does and must not be reachable by a
    field that merely stopped being an object."""
    for bad in ("boom", 7, [], None, True):
        line = json.dumps({"timestamp": _stamp(NOW - 5), "type": "event_msg",
                           "payload": {"type": "task_complete", "error": bad}})
        assert codex_cli.parse_rollout_state([line], NOW) == base.STATE_IDLE, bad


def test_a_task_complete_without_an_error_is_still_idle():
    """The ordinary case, and every rollout ever captured on this machine."""
    assert codex_cli.parse_rollout_state(
        [turn_line("task_complete", _stamp(NOW - 5))], NOW) == base.STATE_IDLE


def test_an_aborted_turn_is_still_idle_whatever_its_reason():
    """All four TurnAbortReason values are things the person did: Esc, a new
    message typed over the turn, a review closing, a budget stopping it.
    Idle is the right colour for all four, and this mapping is frozen."""
    for reason in ("interrupted", "replaced", "review_ended", "budget_limited"):
        line = json.dumps({"timestamp": _stamp(NOW - 5), "type": "event_msg",
                           "payload": {"type": "turn_aborted",
                                       "reason": reason}})
        assert codex_cli.parse_rollout_state([line], NOW) == base.STATE_IDLE, \
            reason


def test_an_abandoned_failed_turn_claims_nothing():
    """A failure an hour ago is a session that is gone, not a red light that
    stays on until the daemon restarts."""
    gone = codex_cli.ABANDONED_AFTER_S + 1
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - gone))], NOW) == base.STATE_UNKNOWN


def test_a_failed_session_is_the_worst_state_and_counted_with_the_stuck(tmp_path):
    """The wire has one count for "not working and not finished", and
    claude_state.poll folds failed into it for the same reason: `state`
    already carries which of the two it is."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Blink"),
                         token_count_line(rate_limits()),
                         failed_line(_stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[meta_line("/Users/K/Other"),
                         turn_line("task_started", _stamp(NOW - 9))])
    st = _state_frame(root)
    assert (st.state, st.n_stuck, st.n_run, st.n_idle) == ("failed", 1, 1, 0)
    assert st.n_sessions() == 2
    assert st.label == "Blink"      # the only session holding the state
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/pc/test_codex_cli.py -q -k "died_on_a_usage or terminal_error or struct_shaped or not_call_a_turn_failure or no_info_is_failed or unexpected_shape or counted_with_the_stuck"`
Expected: FAIL — `assert 'idle' == 'failed'`.

- [ ] **Step 3: Write the implementation**

Replace the `_TURN_EVENTS` block (`pc/providers/codex_cli.py:261-266`) with:

```python
STATE_SRC_ID = "cli-state"
_TURN_EVENTS = {
    "task_started": base.STATE_RUNNING,
    "task_complete": base.STATE_IDLE,
    "turn_aborted": base.STATE_IDLE,
}

# `turn_aborted` is NOT a failure, and the temptation to make it one is why
# this paragraph exists. Its `reason` is one of `interrupted`, `replaced`,
# `review_ended` or `budget_limited` (protocol.rs TurnAbortReason): Esc, a
# new message typed over the running turn, a review closing, a budget
# stopping it. Every one of those is something the PERSON did, and idle --
# "finished, your turn" -- is the right colour for all four. Red is reserved
# for what the model or the API did.
#
# That is `task_complete` carrying an `error`. TurnCompleteEvent.error is
# documented upstream as "Terminal error details when the turn completed
# unsuccessfully", is skipped entirely on success, and its CodexErrorInfo
# includes UsageLimitExceeded -- which is the single event this product
# exists to warn about, and the mirror of Claude's StopFailure error:
# "rate_limit".
#
# NEVER OBSERVED. Every rollout captured on the machine this was written
# against is a success, so this branch is pinned to the upstream schema and
# to tests/ci/check_codex_contract.sh rather than to a real file. It is
# therefore written to degrade toward idle: an `error` that is not an object
# leaves the mapping above exactly as it was.

# The two errors upstream itself says do not fail a turn
# (CodexErrorInfo::affects_turn_status). Both are failures of a client
# OPERATION rather than of the turn, and painting the panel red because a
# thread rollback failed would cry wolf with the one colour that must not.
#
# A deny-list rather than an allow-list, and that direction is deliberate: a
# variant added upstream tomorrow lands on the failing side, which is where
# `Other` already is. A unit variant serialises as a bare snake_case string
# and a struct variant as a single-key object, so both shapes are checked.
_NOT_A_TURN_FAILURE = ("thread_rollback_failed", "active_turn_not_steerable")


def _is_turn_failure(error) -> bool:
    """Does this `task_complete` error mean the turn itself failed?"""
    if not isinstance(error, dict):
        return False
    info = error.get("codex_error_info")
    if isinstance(info, str):
        return info not in _NOT_A_TURN_FAILURE
    if isinstance(info, dict) and len(info) == 1:
        return next(iter(info)) not in _NOT_A_TURN_FAILURE
    # Absent, null, or a shape this version has never seen. Upstream's rule
    # for a missing CodexErrorInfo is that the turn failed
    # (ErrorEvent::affects_turn_status is is_none_or), and so is this: an
    # error object with nothing legible in it is still an error object.
    return True
```

Then, inside `parse_rollout_state`, replace:

```python
        state = _TURN_EVENTS.get(payload.get("type"))
        if state is None:
            continue
```

with:

```python
        kind = payload.get("type")
        state = _TURN_EVENTS.get(kind)
        if state is None:
            continue
        if kind == "task_complete" and _is_turn_failure(payload.get("error")):
            state = base.STATE_FAILED
```

Finally, in `CodexCliProvider.poll`, replace the `n_stuck` line:

```python
                n_stuck=counts.get(base.STATE_STUCK, 0),
```

with:

```python
                # Folded together, exactly as claude_state.poll folds them:
                # the wire has one count for "not working and not finished",
                # and `state` above already says which of the two it is.
                # Mapping failed to nothing would leave a failed session
                # reporting zero -- "Session failed" where the panel should
                # say "Session failed - 2 sessions".
                n_stuck=(counts.get(base.STATE_STUCK, 0)
                         + counts.get(base.STATE_FAILED, 0)),
```

Also update the module's execution-state docstring paragraph at lines 246-257 — the sentence "`task_complete` when the answer is in" is now only half true. Replace that paragraph's second sentence with:

```python
# Codex has no hook interface, but its rollout log is a journal of the same
# transitions: `task_started` when a turn begins, `task_complete` when the
# answer is in -- or, when that record carries an `error`, when the turn died
# instead -- and `turn_aborted` when the person stopped it. The newest of
# these in a file is that session's state, aged by its own timestamp, with
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_codex_cli.py -q`
Expected: PASS, no failures.

- [ ] **Step 5: Run the whole suite**

Run: `pytest tests -q`
Expected: previous total + 10 passed.

- [ ] **Step 6: Commit**

```bash
git add pc/providers/codex_cli.py tests/pc/test_codex_cli.py
git commit -m "feat: a Codex turn that died on an error is failed, not finished"
```

---

### Task 7: Two named providers, one line

**Files:**
- Test only: `tests/pc/test_ingest.py`
- Modify: nothing. `pc/ingest._pair_from` (line 179) already implements the rule; this task proves it holds now that Codex can also carry a name.

**Interfaces:**
- Consumes: the `named()` helper at `tests/pc/test_ingest.py:117` and `Fixed` at line 22.
- Produces: nothing.

**The rule, stated.** The label survives only when exactly one frame is in the state the panel is actually showing — `worst_of(primary, secondary)`, because `protocol.frame_to_usage` sends one light for the whole desk — *and* the summed count for that state is 1. There is no Claude-over-Codex precedence and there must not be: choosing between two projects that are equally true of one line puts a wrong sentence on the panel, and a line nobody can trust is worse than no line. Until this plan, only Claude could set `label`, so the both-named case was unreachable and untested.

- [ ] **Step 1: Write the failing test**

Add to `tests/pc/test_ingest.py`, immediately after `test_a_second_provider_in_a_worse_state_takes_the_name_away`:

```python
def test_two_named_sessions_in_the_same_state_leave_the_board_unnamed():
    """Codex can name a session now, so both providers can arrive holding a
    name for the same state. There is one line and no honest rule for
    choosing between two projects that are equally true of it, so neither is
    shown -- the same refusal each provider already applies within itself,
    applied once more across them. The count carries the meaning.
    """
    bus = ingest.IngestionBus(
        providers=[Fixed(named(provider="claude", state=base.STATE_WAITING,
                               label="LiveClaudeUi", n_wait=1)),
                   Fixed(named(provider="codex", at=NOW - 30,
                               state=base.STATE_WAITING,
                               label="Blink", n_wait=1))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["state"] == "waiting"
    assert msg["n_wait"] == 2
    assert bus.session_pair() == ("", 2)


def test_a_codex_name_shows_when_it_is_the_only_holder_of_the_state():
    """The other half of the same rule, and the one that would be lost by a
    lazy "Claude wins" precedence: a Claude session merely working beside a
    waiting Codex session leaves exactly one holder of the state the panel is
    showing, and that one is named whichever provider it came from.
    """
    bus = ingest.IngestionBus(
        providers=[Fixed(named(provider="claude", state=base.STATE_RUNNING,
                               label="LiveClaudeUi", n_run=1)),
                   Fixed(named(provider="codex", at=NOW - 30,
                               state=base.STATE_WAITING,
                               label="Blink", n_wait=1))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["state"] == "waiting"
    assert bus.session_pair() == ("Blink", 1)


def test_a_failed_codex_session_outranks_a_waiting_claude_one():
    """base.SEVERITY puts failed first, and protocol.frame_to_usage sends
    worst_of(primary, secondary). A Codex turn that died on a usage limit is
    the loudest thing on the desk and takes the line, name and all.
    """
    bus = ingest.IngestionBus(
        providers=[Fixed(named(provider="claude", state=base.STATE_WAITING,
                               label="LiveClaudeUi", n_wait=1)),
                   Fixed(named(provider="codex", at=NOW - 30,
                               state=base.STATE_FAILED,
                               label="Blink", n_stuck=1))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["state"] == "failed"
    assert bus.session_pair() == ("Blink", 1)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/pc/test_ingest.py -q -k "two_named_sessions or only_holder or outranks_a_waiting"`
Expected: **PASS**, all three. This is the one place in the plan where the test is expected to pass on first run, and that is the finding: `_pair_from` already generalises correctly and Task 5 needed no change in `pc/ingest.py`. If any of the three FAILS, stop — `_pair_from` does not generalise, and Task 5 shipped a label the bus mishandles. Report the failure rather than editing `ingest.py` to suit the test.

- [ ] **Step 3: There is no implementation step**

Nothing to write. Confirm by inspection that `pc/ingest.py` and `pc/normalizer.py` are unmodified:

Run: `git diff --stat pc/ingest.py pc/normalizer.py`
Expected: no output.

- [ ] **Step 4: Run the whole suite**

Run: `pytest tests -q`
Expected: previous total + 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/pc/test_ingest.py
git commit -m "test: pin what happens when both providers name a session"
```

---

### Task 8: The contract script watches the new fields

**Files:**
- Modify: `tests/ci/check_codex_contract.sh` (append before the final `printf 'PASS ...'`)
- Test: the script is the test.

**Interfaces:**
- Consumes: `fetch()`, `ok()` and `fail()` from the script and `tests/ci/lib.sh`.
- Produces: nothing consumed elsewhere.

**Why this task is not optional.** The reader now depends on five upstream names it did not depend on before, and two of them carry a live rename warning: `#[serde(rename = "task_started", alias = "turn_started")]` and the same for `task_complete`. The day the alias becomes the primary name, the state machine goes silent and every test on `tests/pc/test_codex_cli.py` stays green, because they all feed the reader strings this repo wrote. Every pattern below was verified against `openai/codex@main` on 2026-09-03.

- [ ] **Step 1: Write the failing check**

Append to `tests/ci/check_codex_contract.sh`, immediately before the final `printf 'PASS [codex contract at %s]\n' "$REF"` line:

```sh
# 7. The turn events pc/providers/codex_cli.parse_rollout_state keys on.
#    The aliases are upstream telling us a rename is coming: the v2 wire
#    spells these turn_started/turn_complete. The day the alias becomes the
#    primary name the state machine goes silent and every Python test stays
#    green, because they all feed the reader strings this repo wrote.
grep -q '#\[serde(rename = "task_started", alias = "turn_started")\]' "$FILE" ||
	fail "task_started is no longer the serialised name -- see the turn_started alias"
grep -q '#\[serde(rename = "task_complete", alias = "turn_complete")\]' "$FILE" ||
	fail "task_complete is no longer the serialised name -- see the turn_complete alias"
ok "task_started and task_complete are still the wire names"

# 8. Failure. The reader calls a turn failed when its task_complete carries
#    an `error` object, and UsageLimitExceeded is the case it exists for.
grep -q 'pub struct TurnCompleteEvent' "$FILE" || fail "TurnCompleteEvent is gone"
grep -q 'pub error: Option<ErrorEvent>' "$FILE" ||
	fail "TurnCompleteEvent no longer carries error: Option<ErrorEvent>"
grep -q 'pub codex_error_info: Option<CodexErrorInfo>' "$FILE" ||
	fail "ErrorEvent no longer carries codex_error_info"
grep -q 'UsageLimitExceeded,' "$FILE" ||
	fail "CodexErrorInfo lost UsageLimitExceeded -- the case this reader exists for"
ok "task_complete still reports failure through error/codex_error_info"

# 9. The two errors that do NOT fail a turn. codex_cli._NOT_A_TURN_FAILURE
#    mirrors this exact arm; a variant leaving it would have the panel go red
#    for something upstream does not call a failure, and a variant joining it
#    would have us keep painting red for something that stopped being one.
grep -A2 'pub fn affects_turn_status' "$FILE" |
	grep -q 'Self::ThreadRollbackFailed | Self::ActiveTurnNotSteerable { \.\. } => false,' ||
	fail "affects_turn_status' not-a-failure arm changed -- see codex_cli._NOT_A_TURN_FAILURE"
ok "thread_rollback_failed and active_turn_not_steerable are still not turn failures"

# 10. turn_aborted stays a user action. All four reasons are things the
#     person did, which is why the reader maps every one of them to idle
#     rather than to red.
grep -q 'pub enum TurnAbortReason' "$FILE" || fail "TurnAbortReason is gone"
for reason in Interrupted Replaced ReviewEnded BudgetLimited; do
	grep -A5 'pub enum TurnAbortReason' "$FILE" |
		grep -q "^[[:space:]]*$reason,$" ||
		fail "TurnAbortReason lost $reason -- re-read whether turn_aborted still means idle"
done
ok "turn_aborted still means the person stopped the turn"

# 11. The project name. Line 1 of a rollout is a session_meta record, and its
#     cwd is the only place a Codex session's name can come from -- the
#     filename carries a timestamp and a UUID and nothing else.
grep -q 'pub struct SessionMeta' "$FILE" || fail "SessionMeta is gone"
grep -A20 'pub struct SessionMeta' "$FILE" | grep -q 'pub cwd: PathBuf' ||
	fail "SessionMeta no longer carries cwd -- Codex sessions cannot be named"
ok "session_meta still carries cwd"

POLICY=$(fetch codex-rs/rollout/src/policy.rs)
grep -q 'RolloutItem::SessionMeta(_) => true' "$POLICY" ||
	fail "session_meta is no longer persisted unconditionally"
ok "session_meta is still written to every rollout"
```

- [ ] **Step 2: Run the script to verify the new checks execute**

Run: `sh tests/ci/check_codex_contract.sh`
Expected: `PASS [codex contract at main]`, with the six new `ok` lines visible above it. If it fails on the `fetch` of `codex-rs/rollout/src/policy.rs`, confirm the path — it returned HTTP 200 on 2026-09-03 and `fetch()` names the local copy `rollout-policy.rs`, which does not collide with `protocol.rs`.

- [ ] **Step 3: Prove a check can actually fail**

A contract check that cannot go red is decoration. Verify one:

```bash
mkdir -p /tmp/blink-codex-src
sh tests/ci/check_codex_contract.sh >/dev/null 2>&1   # populate the cache
cp "${TMPDIR:-/tmp}/blink-codex-contract/"* /tmp/blink-codex-src/
sed -i.bak 's/pub cwd: PathBuf/pub working_dir: PathBuf/' /tmp/blink-codex-src/protocol.rs
CODEX_SRC_DIR=/tmp/blink-codex-src sh tests/ci/check_codex_contract.sh
```

Expected: exits non-zero with `SessionMeta no longer carries cwd -- Codex sessions cannot be named`. Then `rm -rf /tmp/blink-codex-src`.

- [ ] **Step 4: Commit**

```bash
git add tests/ci/check_codex_contract.sh
git commit -m "test: watch the rollout fields the name and the failure state now need"
```

---

### Task 9: Everything green together

**Files:** none — this task changes nothing. It is the gate.

**Interfaces:**
- Consumes: every task above.
- Produces: the evidence for the completion claim.

- [ ] **Step 1: Run the full Python suite**

Run: `pytest tests -q`
Expected: `<baseline + 42> passed`, no failures, no errors. (3 + 5 + 11 + 4 + 6 + 10 + 3 = 42 new tests. With the 593 baseline that is 635; with the 561 baseline, 603.)

- [ ] **Step 2: Run the contract script**

Run: `sh tests/ci/check_codex_contract.sh`
Expected: `PASS [codex contract at main]`.

- [ ] **Step 3: Run the firmware host tests**

Run: `sh tests/ci/check_host_tests.sh`
Expected: `PASS [host tests]`. No firmware source changed in this plan; this confirms that. In particular `fmt` must still report its `"Working - caf?"` check, which is what Task 1's argument rests on.

- [ ] **Step 4: Run the hook shim contract**

Run: `sh tests/ci/check_hook_shim.sh sh`
Expected: pass. No shim changed here either — the Claude shim's `_projname` rule (`[0-9A-Za-z][0-9A-Za-z._-]{0,23}`, so no spaces and no non-ASCII) is a **separate open owner decision** and this plan deliberately does not touch it. What this plan does change is the argument: with `encode` no longer escaping, the wire and the firmware can both carry a non-ASCII name, so the shim's refusal is now the only thing stopping a Claude project called `café` from being named. Report that; do not act on it.

- [ ] **Step 5: Confirm the blast radius**

Run: `git diff --stat main...HEAD`
Expected: exactly six files — `pc/protocol.py`, `pc/providers/codex_cli.py`, `tests/pc/test_protocol.py`, `tests/pc/test_codex_cli.py`, `tests/pc/test_ingest.py`, `tests/ci/check_codex_contract.sh`. Anything else means a task went outside its scope.

- [ ] **Step 6: Commit nothing**

There is nothing to commit. If `git status` is not clean, a previous task left work uncommitted — find it and fold it into the commit it belongs to.

---

## Deliberately not done

Named here so a reviewer can see they were considered rather than missed.

- **`waiting` for Codex.** Approval requests are in upstream's never-persisted arm of `should_persist_event_msg`, and no approval event appears in any real rollout. It needs the new Codex hooks system and its own plan. A permission prompt shows as `running` until it is answered — honest, if less useful.
- **`budget_limited` as a failure.** The research called it "arguably failed". It is unobserved, the other three `TurnAbortReason` values are unambiguously user actions, and the owner froze the `turn_aborted` mapping. Splitting one unobserved value out of a frozen mapping is scope creep.
- **The Claude hook shim's name rules.** See Task 9 Step 4.
- **A Codex name in `blink status`.** `pc/cli.py:1357-1367` prints only the reading's age. Adding a name there is cosmetic and belongs with the `blink status` Board line already queued for v1.2.3.
- **A Codex label with no rate-limit reading.** `normalizer.merge` returns `None` for a provider carrying no percentage, so a Codex state frame — and now its label — only reaches the board when some rollout also has a `token_count` line. That is pre-existing behaviour of the state frame, not something this plan introduces, and changing it would mean changing when a provider is allowed to speak at all.

---

## Self-review

**1. Spec coverage.** Research Q1 (naming) → Tasks 2, 3, 4, 5. Q3 (failure) → Task 6, with the `turn_aborted`-stays-idle correction pinned by a test in Task 6 Step 1 and by the contract check in Task 8. Q3's note that the contract script has gaps around `task_started`/`task_complete`/`TurnAbortReason`/`TurnComplete.error` → Task 8. The 51 MB-file warning → Task 2's separate head read and Task 4's cache. The "label policy should mirror claude_state.py" note → Task 5's one-holder rule and Task 7's cross-provider tests. Q2 is out of scope by instruction and is listed under "Deliberately not done". No gaps.

**2. Placeholder scan.** Every code step carries the code. Every test step carries the test body. No "similar to Task N", no "add error handling", no TBD. Task 7 has no implementation step and says so explicitly with a reason and a verification command, rather than leaving a blank. Task 9 has no code by design and says what it is instead.

**3. Type consistency.** Checked across tasks: `HEAD_BYTES` (Task 2) is used by name in Task 2's tests and by `_head_line` only. `_head_line(path) -> str` (Task 2) is consumed by `_name_for` (Task 4) and monkeypatched by name in Task 4's tests — the module-level lookup in `_name_for` is what makes that patch work, so `_name_for` must call `_head_line(path)` and not hold a reference. `session_meta_cwd(head_line) -> str | None` and `_project_name(cwd) -> str` (Task 3) compose in that order in Task 4; `_project_name` accepts `None` because `session_meta_cwd` can return it, and Task 3's `test_a_cwd_that_is_not_a_string_is_refused_not_raised` pins that. `_prune_names(known)` takes a set; Task 5 passes `set(paths)` and Task 4's test passes a set literal. `_is_turn_failure(error) -> bool` and `_NOT_A_TURN_FAILURE` (Task 6) are used only inside `parse_rollout_state`. `STATE_SRC_ID` is referenced by Task 5's `_state_frame` helper, which Task 6's last test also uses — so Task 6 depends on Task 5 having landed, which the strict ordering guarantees. `meta_line()` is defined in Task 4 and used in Tasks 5 and 6; `failed_line()` is defined in Task 6 and used only there; `turn_line()` and `_stamp()` already exist at `tests/pc/test_codex_cli.py:379` and `:384`.

**Fixed inline during review:** Task 4's test originally patched `codex_cli._head_line` without restoring it, which would have leaked into every later test in the file — wrapped in `try/finally`. Task 6's `_state_frame` was originally re-defined in Task 6, shadowing Task 5's; it now reuses Task 5's. Task 5's `poll` rewrite originally left `base.worst_of(counts)` inline in the constructor, which made the label's `counts.get(state, 0)` unable to see the winning state; it is hoisted to a local first. Task 2's head-bound test originally asserted `HEAD_BYTES > 19 * 1024`, which a 20 KB bound would pass while failing on the first rollout with a longer `base_instructions`; it now demands several times the observation, matching the constant's own comment.
