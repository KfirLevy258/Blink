# Codex Hook Shim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Codex sessions the same "waiting for you" and per-session census that Claude Code sessions already have, by registering Blink's existing hook shim with Codex's own lifecycle hooks and reading the result out of a state directory of its own.

**Architecture:** Codex 0.150.0 ships a hooks system whose event names are deliberately the same words Claude Code uses (`PreToolUse`, `PermissionRequest`, `PostToolUse`, `Stop`, `Interrupt`, `SessionStart`, `SessionEnd`, `SubagentStart`, `SubagentStop`, `UserPromptSubmit`) and whose command hooks are fed `session_id` and `cwd` on stdin — the exact two fields `tools/blink-hook.sh` reads. So the shim is not rewritten; it takes one extra argument that selects `~/.blink/state-codex/` instead of `~/.blink/state/`, and every sanitiser in it stays single-sourced. On the daemon side the hook slots do not become a third provider — `pc/normalizer.select_pair` shows only two rings and drops a third — they are unioned into the existing `codex` provider's state frame, keyed by session id, with the hook's answer beating the rollout reader's for any session both describe.

**Tech Stack:** POSIX sh, Python 3.10+, pytest. No new dependencies — in particular no TOML library, which is why Task 1's discovery matters so much.

**Spec:** `/private/tmp/claude-502/-Users-KfirLevy-Projects-LiveClaudeUi/aeb3001d-3255-41ed-b053-ecf8e0cdec4c/scratchpad/codex-research.md` (the Q2 section and the "Extras" section). Task 1 copies the parts this plan depends on into `docs/research/codex-hook-contract.md` so the argument travels with the repository.

## Global Constraints

- **A separate state directory.** Codex hook slots are written to `~/.blink/state-codex/`; Claude's stay in `~/.blink/state/`. `pc/providers/claude_state.STATE_DIR` is never pointed at the Codex directory, and no code path counts a file from one directory into the other provider's numbers.
- **`pc/protocol.frame_to_usage` sums a primary and a secondary frame only** (`pc/protocol.py:381-384`). Codex hook counts must therefore arrive on the *existing* `provider="codex"` frame, not on a new provider id. A third provider is silently dropped by `pc/normalizer.select_pair`.
- **A waiting state that cannot be cleared is worse than no waiting state at all.** Every event that sets `waiting` is registered together with every event that clears it, and each clearing path has a test that asserts the clear.
- **The shim is POSIX sh, runs on every tool call, and fails silently.** No stdout, no stderr, exit 0 always, and an unwritable `$HOME` produces nothing at all. `tests/ci/check_hook_shim.sh` is the proof.
- **Shim files are written with `newline="\n"`** (`pc/cli.py:170`) — Windows CRLF broke `case ... in\r` on every tool call once already.
- **Never rewrite a file we cannot parse.** `pc/install_statusline.SettingsUnreadable` is raised and the caller reports `skipped (...)` and keeps going, exactly as `pc/cli.py` already does for the Claude hooks step.
- **The Codex hook installs automatically when Codex is detected**, not behind a flag — but `blink install` says, before it writes anything, that Codex will ask the user to trust the hook once.
- **UI copy is sentence case:** every on-screen sentence starts with a capital letter.
- **Python 3.10+**, matching the style in `pc/`. Comments explain WHY, in prose, at the density of the surrounding file.
- **`pytest tests -q` passes (515 today) and `sh tests/ci/check_hook_shim.sh sh` passes at every commit.**

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `docs/research/codex-hook-contract.md` | what the installed Codex actually accepts | create (Task 1) |
| `tools/blink-hook.sh` | the one shim, for both tools | second argument selects the state directory |
| `tests/ci/check_hook_shim.sh` | the shim's whole security battery | a Codex-directory section |
| `pc/providers/claude_state.py` | the shared state machine and slot scanner | `Interrupt` clears; expose `session_states()` |
| `pc/providers/codex_state.py` | scan `~/.blink/state-codex/` | create |
| `pc/providers/codex_cli.py` | the `codex` provider's frames | read a rollout's session id; union hook state over rollout state |
| `pc/install_codex_hooks.py` | register/unregister in Codex's hooks file | create |
| `pc/cli.py` | install, uninstall, status | a new step with its disclosure; cleanup; a status line |
| `tests/pc/test_state_machine.py` | the state machine | `Interrupt`, `session_states()` |
| `tests/pc/test_codex_state.py` | the Codex slot scanner | create |
| `tests/pc/test_codex_cli.py` | the Codex provider | the union |
| `tests/pc/test_install_codex_hooks.py` | registration | create |
| `tests/test_cli.py` | the commands | install/uninstall/status wiring |

Task 1 is a gate: Tasks 8–10 are written against a contract it pins, and each of them names the exact finding it depends on. Tasks 2–7 (the shim and the daemon side) do not depend on Task 1 and can proceed in parallel with it. Task 14 is the only task that can call any of this done.

---

### Task 1: Pin the Codex hook contract against the installed binary

Nobody has ever executed a Codex hook — not on this desk, not anywhere. Everything after this task is written against a contract, so the contract gets established once, written down, and committed, rather than being guessed at inside four different files.

**Files:**
- Create: `docs/research/codex-hook-contract.md`
- Test: none. This task produces evidence, not behaviour; Task 14 is where the evidence is confirmed by execution.

**Interfaces:**
- Produces: findings **F1** (hooks file path), **F2** (top-level JSON shape), **F3** (whether `~/.codex/config.toml` needs a pointer key), **F4** (matcher-group shape), **F5** (trust mechanism and where the trust record lives). Tasks 8, 9 and 10 each name the finding they consume.

- [ ] **Step 1: Collect what the installed binary compiles in**

The binary is at `/Users/KfirLevy/.local/bin/codex` (a symlink into `~/.codex/packages/standalone/current/bin/codex`), version 0.150.0.

```bash
B=$(readlink -f /Users/KfirLevy/.local/bin/codex || echo /Users/KfirLevy/.local/bin/codex)
strings -a "$B" | grep -oE '.{0,120}hooks/hooks\.json.{0,120}' | sort -u
strings -a "$B" | grep -oE '.{0,200}HookEventsToml.{0,200}' | sort -u
strings -a "$B" | grep -oE '.{0,200}MatcherGroup.{0,200}' | sort -u
strings -a "$B" | grep -oE '.{0,200}HooksFile.{0,200}' | sort -u
strings -a "$B" | grep -oE '.{0,200}trusted_hash.{0,200}' | sort -u
strings -a "$B" | grep -iE 'Failed to trust hooks|failed to write hook trust|updating hook trust' | sort -u
```

Expected, from the probe already run while writing this plan (reproduce it, do not take it on faith):
`hooks/hooks.json`, `"hooks": "./hooks.json"`, `struct HooksFile`, `struct HooksToml`, `struct HookEventsToml`, `struct MatcherGroup` with fields `matcher` and `hooks`, `internally tagged enum HookHandlerConfig` with variants `Command|Prompt|McpTool|Agent` and fields `type`, `command`, `commandWindows`, `struct HookStateToml` with `trusted_hash`, and the trust strings `Failed to trust hooks:`, `failed to write hook trust:`, `config/batchWrite failed while updating hook trust in TUI`.

- [ ] **Step 2: Confirm against upstream source**

```bash
mkdir -p /tmp/codex-src && cd /tmp/codex-src
curl -fsSL -o hooks_schema.rs https://raw.githubusercontent.com/openai/codex/main/codex-rs/hooks/src/schema.rs
curl -fsSL -o config_types.rs https://raw.githubusercontent.com/openai/codex/main/codex-rs/config/src/types.rs
grep -n 'HooksFile\|HooksToml\|HookEventsToml\|MatcherGroup\|trusted_hash\|managed_dir' hooks_schema.rs config_types.rs
```

If a file 404s, find it with `curl -fsSL "https://api.github.com/search/code?q=HookEventsToml+repo:openai/codex"` or clone the repo shallowly. Record which source you actually read.

- [ ] **Step 3: Confirm the file location on this machine**

```bash
ls -la "${CODEX_HOME:-$HOME/.codex}"
ls -la "${CODEX_HOME:-$HOME/.codex}/hooks" 2>&1
cat "${CODEX_HOME:-$HOME/.codex}/config.toml"
```

Today `~/.codex/hooks/` does not exist and `config.toml` holds only a `[projects."..."]` table. Both facts matter: F1 has to say whether Codex reads a hooks file that is absent by default (so we create the directory), and F3 has to say whether an absent `hooks` key in `config.toml` means "no hooks" or "the default path".

- [ ] **Step 4: Write the findings down**

Create `docs/research/codex-hook-contract.md` with exactly these headings and no others, each answered in prose with the evidence beside it, and each marked **VERIFIED** (observed on this machine) or **READ** (upstream source only):

```markdown
# Codex hook contract, as of codex-cli 0.150.0

Everything here is what `blink install` writes into another vendor's
configuration, so each line says how it was established. Re-run the probes in
`docs/plans/codex-hook-shim.md` Task 1 after any Codex upgrade.

## F1 — Where the hooks file lives
<path, and whether Codex creates it or we must>

## F2 — The top-level JSON shape
<either `{"<Event>": [...]}` at the top level, or `{"hooks": {"<Event>": [...]}}`>

## F3 — Does config.toml need a pointer
<yes/no, and the exact key and value if yes>

## F4 — The matcher-group and handler shape
<the exact JSON one registered command hook is written as>

## F5 — Trust
<what Codex prompts, when, where the trust record is stored, and what
invalidates it>

## What could not be established without running one
<carry forward anything still open into Task 14's checklist>
```

- [ ] **Step 5: Commit**

```bash
git add docs/research/codex-hook-contract.md
git commit -m "docs: pin the Codex hook contract against the installed 0.150.0"
```

---

### Task 2: The shim's state directory becomes an argument

**Files:**
- Modify: `tools/blink-hook.sh:31-35` and `tools/blink-hook.sh:112`
- Test: `tests/ci/check_hook_shim.sh` (append a section 14, before the final `printf 'PASS ...'`)

**Interfaces:**
- Produces: the shim invocation `sh <shim> <Event> codex` writes to `$HOME/.blink/state-codex/`; `sh <shim> <Event>` and any unrecognised second argument write to `$HOME/.blink/state/` as before.

- [ ] **Step 1: Write the failing test**

Append to `tests/ci/check_hook_shim.sh`, immediately before the closing `printf 'PASS [%s]\n' "$WHICH"` line:

```sh
# 14. The Codex state directory. The same shim serves Codex's hooks, which use
#     the same event names and the same stdin fields; the second argument is
#     the only thing that differs, and it must move every write.
CODEXDIR="$HOME/.blink/state-codex"
out=$(printf '%s' "$PAYLOAD" | $SH "$SHIM" PreToolUse codex 2>"$WORK/err14.txt")
[ -z "$out" ] || fail "codex run printed to stdout: [$out]"
[ -s "$WORK/err14.txt" ] && fail "codex run wrote to stderr: $(cat "$WORK/err14.txt")"
grep -q '"event":"PreToolUse"' "$CODEXDIR/abc-123.state" ||
	fail "codex event not recorded under state-codex"
ok "the codex argument writes under ~/.blink/state-codex"

# 14b. ...and never into the Claude directory. A Codex session counted as a
#      Claude one is the entire reason the second directory exists: the pip
#      row would attribute it to the wrong tool and the wrong account.
printf '{"session_id":"codex-only"}' | $SH "$SHIM" Stop codex >/dev/null 2>&1
[ -f "$CODEXDIR/codex-only.state" ] || fail "codex session not recorded"
[ ! -e "$DIR/codex-only.state" ] ||
	fail "a codex session leaked into the Claude directory"
printf '{"session_id":"claude-only"}' | $SH "$SHIM" Stop >/dev/null 2>&1
[ ! -e "$CODEXDIR/claude-only.state" ] ||
	fail "a Claude session leaked into the codex directory"
ok "the two state directories never cross"

# 14c. An unrecognised second argument falls back to the Claude directory
#      rather than becoming a path fragment. This value is written by our own
#      installer, but it lands in a config file a person can hand-edit, and
#      nothing hand-edited gets to choose a directory under $HOME.
printf '{"session_id":"weird"}' | $SH "$SHIM" Stop '../../../etc' >/dev/null 2>&1
[ -f "$DIR/weird.state" ] ||
	fail "an unknown tool argument did not fall back to state/"
extra=$(ls -1 "$HOME/.blink" | grep -vxE 'state|state-codex|precious' || true)
[ -z "$extra" ] ||
	fail "an unknown tool argument created something in ~/.blink: $extra"
ok "an unknown tool argument cannot choose a directory"

# 14d. Every sanitiser case the Claude directory has, repeated for the codex
#      one. The sanitisers are shared code, but the directory they write into
#      is not, and this is the file that has to prove the second one is as
#      safe as the first.
printf 'keep me' > "$HOME/.blink/precious"
printf '{"session_id":"../../pwned"}' |
	$SH "$SHIM" PreToolUse codex >/dev/null 2>&1
[ ! -e "$HOME/pwned.state" ] ||
	fail "codex PATH TRAVERSAL: wrote outside the state dir"
[ -f "$CODEXDIR/unknown.state" ] ||
	fail "codex traversal did not fall back to 'unknown'"
for bad in '.' '..'; do
	printf '{"session_id":"%s","agent_id":"precious"}' "$bad" |
		$SH "$SHIM" SubagentStart codex >/dev/null 2>&1
	printf '{"session_id":"%s","agent_id":"precious"}' "$bad" |
		$SH "$SHIM" SubagentStop codex >/dev/null 2>&1
	printf '{"session_id":"%s"}' "$bad" |
		$SH "$SHIM" SessionEnd codex >/dev/null 2>&1
done
[ "$(cat "$HOME/.blink/precious")" = "keep me" ] ||
	fail "a dot session id reached ~/.blink through the codex argument"
[ -d "$CODEXDIR" ] || fail "a dot session id removed the codex state directory"
[ -z "$(find "$CODEXDIR" -prune \( -perm -040 -o -perm -004 \) -print)" ] ||
	fail "codex state directory readable by others: $(ls -ld "$CODEXDIR")"
ok "the codex directory is as sanitised and as private as the Claude one"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `sh tests/ci/check_hook_shim.sh sh`
Expected: FAIL `codex event not recorded under state-codex` — the shim ignores `$2` today and wrote to `~/.blink/state/`.

- [ ] **Step 3: Implement the minimal change**

In `tools/blink-hook.sh`, replace:

```sh
event=${1:-unknown}
input=$(cat)
```

with:

```sh
event=${1:-unknown}

# WHICH TOOL'S SESSIONS these are, from $2, also written by the installer.
#
# Codex grew a hooks interface whose event names are deliberately the same
# words Claude Code uses -- PreToolUse, PostToolUse, PermissionRequest, Stop,
# SessionEnd, SubagentStart/Stop -- and whose command hooks are handed the
# same session_id and cwd on stdin. Everything below therefore already works
# for it unchanged, and the only thing that has to differ is WHERE the slots
# are written: a Codex session counted out of ~/.blink/state would be reported
# to the board as a Claude one, on a Claude pip, against a Claude account.
#
# One shim rather than a second copy of this file, because what is valuable
# here is not the case statement below -- it is the two sanitisers, each of
# which is the fix for a bug that reached a real machine. A second copy is a
# second place for the next such fix to be forgotten, and the cost of avoiding
# it is one parameter expansion on a path that already forks sed twice.
#
# A `case` over a fixed list rather than using $2 as a path fragment: it
# arrives from a configuration file a person can hand-edit, and an
# unrecognised value has to fall back rather than name a directory.
case ${2:-} in
codex) sub=state-codex ;;
*) sub=state ;;
esac

input=$(cat)
```

and replace `DIR="$HOME/.blink/state"` with:

```sh
DIR="$HOME/.blink/$sub"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `sh tests/ci/check_hook_shim.sh sh`
Expected: PASS, including the four new `ok` lines.

Run: `sh tests/ci/check_hook_shim.sh dash`
Run: `sh tests/ci/check_hook_shim.sh busybox`
Expected: PASS. If either shell is not installed the script prints `<sh> not installed` and exits 1 — say so in your report rather than treating it as a failure.

Run: `pytest tests -q`
Expected: 515 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/blink-hook.sh tests/ci/check_hook_shim.sh
git commit -m "feat: the hook shim can write a second tool's state directory"
```

---

### Task 3: `Interrupt` clears a waiting session

Codex fires `Interrupt` where Claude Code has no equivalent event. Today `derive_state` does not know the word, so it returns `STATE_UNKNOWN` — and an unknown state means the session is dropped from the census entirely. A person who hits Esc on a Codex approval prompt would watch the pip vanish rather than go amber-then-quiet. This is one of the events that has to clear `waiting`.

**Files:**
- Modify: `pc/providers/claude_state.py:99` (`_IDLE_EVENTS`)
- Test: `tests/pc/test_state_machine.py`

**Interfaces:**
- Consumes: `pc.providers.claude_state.derive_state(event: str, age_s: float) -> str`
- Produces: `derive_state("Interrupt", age) == base.STATE_IDLE` for any age under `ABANDONED_AFTER_S`.

- [ ] **Step 1: Write the failing test**

Append to `tests/pc/test_state_machine.py`:

```python
def test_interrupt_is_a_finished_turn_not_an_unknown_one():
    """Codex fires Interrupt where Claude Code has no equivalent event.

    Idle rather than running or failed: the person pressed Esc, so the turn is
    over and it is their turn again -- which is exactly what idle means here.
    Leaving it unknown was worse than wrong, because unknown drops the session
    out of the census: interrupting a Codex turn made its pip disappear.
    """
    assert claude_state.derive_state("Interrupt", 1.0) == base.STATE_IDLE


def test_interrupt_clears_a_waiting_session():
    """The clear that matters most: a permission prompt the person aborted.

    A waiting state with no path out of it is worse than no waiting state,
    because the panel then calls for attention that nothing can satisfy.
    """
    assert claude_state.derive_state("PermissionRequest", 1.0) == base.STATE_WAITING
    assert claude_state.derive_state("Interrupt", 1.0) != base.STATE_WAITING
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/pc/test_state_machine.py -q -k interrupt`
Expected: FAIL — `derive_state("Interrupt", 1.0)` returns `''` (`STATE_UNKNOWN`), not `'idle'`.

- [ ] **Step 3: Implement**

In `pc/providers/claude_state.py`, change:

```python
_IDLE_EVENTS = ("Stop",)
```

to:

```python
# Interrupt is Codex's word for "the person pressed Esc". It belongs with
# Stop rather than with the failures: an aborted turn is over and it is the
# person's turn again, which is what idle means here. Claude Code never
# sends it, so listing it costs that side nothing.
#
# It is also one of the events that CLEAR a permission prompt. A waiting
# state the panel cannot get out of is worse than no waiting state at all,
# so every way a Codex approval can end -- answered (PostToolUse), abandoned
# (Interrupt), or the turn simply finishing (Stop) -- has to land somewhere
# other than waiting.
_IDLE_EVENTS = ("Stop", "Interrupt")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_state_machine.py -q`
Expected: PASS.

Run: `pytest tests -q`
Expected: 517 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/providers/claude_state.py tests/pc/test_state_machine.py
git commit -m "feat: Interrupt ends a turn rather than erasing the session"
```

---

### Task 4: The slot scanner can report per-session states

The union in Task 7 needs to know *which sessions* the hook slots describe, not just how many are in each state, because a session that both the hook and the rollout reader can see must be counted once. `scan()` collapses that information before anyone sees it, so this splits the per-session pass out and rebuilds `scan()` on top of it. Pure refactor: no behaviour changes.

**Files:**
- Modify: `pc/providers/claude_state.py` (`ClaudeStateProvider.scan`)
- Test: `tests/pc/test_state_machine.py`

**Interfaces:**
- Produces: `ClaudeStateProvider.session_states(now_epoch) -> (dict[str, tuple[str, str]], int)` — `{session_id: (state, name)}` for every live session, plus the total live agent count. Sessions in `STATE_UNKNOWN` are absent from the mapping, and are swept exactly as `scan()` swept them.
- Consumed by: Task 5 (`pc/providers/codex_state.scan`).

- [ ] **Step 1: Write the failing test**

Append to `tests/pc/test_state_machine.py`:

```python
def test_session_states_reports_each_session_by_id(tmp_path):
    """Who is in which state, not just how many.

    The Codex union needs the ids: a session that both the hook slots and the
    rollout reader can see has to be counted once, and an id is the only thing
    the two sources share.
    """
    d = tmp_path / "state"
    d.mkdir()
    now = 1_700_000_000.0
    (d / "sess-a.state").write_text(
        '{"event":"PermissionRequest","t":%d,"name":"Alpha"}' % int(now - 5))
    (d / "sess-b.state").write_text(
        '{"event":"PreToolUse","t":%d}' % int(now - 5))

    prov = claude_state.ClaudeStateProvider(path=str(d), sweep=False)
    states, agents = prov.session_states(now)

    assert states == {"sess-a": (base.STATE_WAITING, "Alpha"),
                      "sess-b": (base.STATE_RUNNING, "")}
    assert agents == 0


def test_session_states_omits_and_sweeps_a_dead_session(tmp_path):
    """An abandoned slot is not a session; it is litter, and it is collected.

    Same rule scan() has always applied -- this asserts the split kept it.
    """
    d = tmp_path / "state"
    d.mkdir()
    now = 1_700_000_000.0
    dead = now - (claude_state.ABANDONED_AFTER_S + 60)
    (d / "gone.state").write_text('{"event":"PreToolUse","t":%d}' % int(dead))

    prov = claude_state.ClaudeStateProvider(path=str(d), sweep=True)
    states, agents = prov.session_states(now)

    assert states == {}
    assert agents == 0
    assert not (d / "gone.state").exists()


def test_scan_still_agrees_with_session_states(tmp_path):
    """The counts are derived from the mapping, so they cannot drift from it."""
    d = tmp_path / "state"
    d.mkdir()
    now = 1_700_000_000.0
    (d / "sess-a.state").write_text(
        '{"event":"PermissionRequest","t":%d,"name":"Alpha"}' % int(now - 5))
    (d / "sess-b.state").write_text(
        '{"event":"PreToolUse","t":%d}' % int(now - 5))

    prov = claude_state.ClaudeStateProvider(path=str(d), sweep=False)
    counts, names, agents = prov.scan(now)

    assert counts == {base.STATE_WAITING: 1, base.STATE_RUNNING: 1}
    assert names == {base.STATE_WAITING: ["Alpha"]}
    assert agents == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/pc/test_state_machine.py -q -k session_states`
Expected: FAIL with `AttributeError: 'ClaudeStateProvider' object has no attribute 'session_states'`.

- [ ] **Step 3: Implement**

In `pc/providers/claude_state.py`, replace the whole body of `scan()` with the two methods below (keep everything above and below it as it is):

```python
    def session_states(self, now_epoch):
        """({session_id: (state, name)}, live agents) for this directory.

        The per-session view, split out of scan() because the Codex union
        needs the ids. A session that both the hook slots and the rollout
        reader can see has to be counted once, and the session id is the only
        thing those two sources have in common -- counts alone cannot be
        de-duplicated.

        Sweeps what it finds dead, exactly as scan() always did: a session
        that ended without SessionEnd firing leaves files nothing else will
        ever look at again.
        """
        try:
            entries = os.listdir(self._dir)
        except OSError:
            # No hooks installed, or nothing has happened yet. Both normal.
            return {}, 0

        states = {}
        agents = 0
        for name in entries:
            if not name.endswith(".state"):
                continue
            sid = name[: -len(".state")]
            state_path = os.path.join(self._dir, name)
            state, age, sess_name = self._read_state(state_path, now_epoch)

            if state is None or state == base.STATE_UNKNOWN:
                # Unreadable, or so old the session is certainly gone. Collect
                # the whole session rather than leaving a directory that will
                # never be looked at again.
                if age is not None and age > ABANDONED_AFTER_S and self._sweep:
                    _unlink(state_path)
                    _rmtree(os.path.join(self._dir, sid))
                continue

            states[sid] = (state, sess_name)
            agents += self._count_agents(os.path.join(self._dir, sid),
                                         now_epoch)
        return states, agents

    def scan(self, now_epoch):
        """{state: n_sessions}, {state: [names]}, total live agents.

        Sweeps what it finds dead."""
        states, agents = self.session_states(now_epoch)
        counts = {}
        names = {}
        for state, sess_name in states.values():
            counts[state] = counts.get(state, 0) + 1
            if sess_name:
                names.setdefault(state, []).append(sess_name)
        return counts, names, agents
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_state_machine.py -q`
Expected: PASS.

Run: `pytest tests -q`
Expected: 520 passed. Any other failure means the refactor changed behaviour — it must not.

- [ ] **Step 5: Commit**

```bash
git add pc/providers/claude_state.py tests/pc/test_state_machine.py
git commit -m "refactor: expose the slot scanner's per-session view"
```

---

### Task 5: `pc/providers/codex_state.py`

**Files:**
- Create: `pc/providers/codex_state.py`
- Test: `tests/pc/test_codex_state.py` (create)

**Interfaces:**
- Consumes: `ClaudeStateProvider.session_states(now_epoch)` from Task 4.
- Produces: `pc.providers.codex_state.STATE_DIR = "~/.blink/state-codex"`, and `scan(now_epoch, path=None, sweep=True) -> (dict[str, str], int)` — `{session_id: state}` and the live agent count.

- [ ] **Step 1: Write the failing test**

Create `tests/pc/test_codex_state.py`:

```python
"""The Codex hook slots: a second directory, read by the same machine."""
import pytest

from pc.providers import base, claude_state, codex_state


def test_the_default_directory_is_not_the_claude_one():
    """The whole point of the second directory.

    Two sessions with the same id are effectively impossible (both tools use
    UUIDs), but the ATTRIBUTION is the real risk: a Codex session read out of
    ~/.blink/state is reported to the board as a Claude one, on a Claude pip,
    against a Claude account's limits.
    """
    assert codex_state.STATE_DIR != claude_state.STATE_DIR
    assert codex_state.STATE_DIR.endswith("state-codex")


def test_scan_reads_the_slots_the_shim_writes(tmp_path):
    d = tmp_path / "state-codex"
    d.mkdir()
    now = 1_700_000_000.0
    (d / "cx-1.state").write_text(
        '{"event":"PermissionRequest","t":%d}' % int(now - 3))
    (d / "cx-2.state").write_text(
        '{"event":"PreToolUse","t":%d}' % int(now - 3))

    states, agents = codex_state.scan(now, path=str(d), sweep=False)

    assert states == {"cx-1": base.STATE_WAITING, "cx-2": base.STATE_RUNNING}
    assert agents == 0


@pytest.mark.parametrize("clearing_event, expected", [
    ("PostToolUse", base.STATE_RUNNING),
    ("UserPromptSubmit", base.STATE_RUNNING),
    ("Stop", base.STATE_IDLE),
    ("Interrupt", base.STATE_IDLE),
])
def test_every_event_that_can_follow_a_prompt_clears_the_wait(
        tmp_path, clearing_event, expected):
    """A waiting state with no way out is worse than no waiting state.

    These are the four events Codex can fire after a PermissionRequest: the
    tool was approved and ran, the person typed something else, the turn
    finished, or the person pressed Esc. Each is asserted separately because
    the failure mode is one of them being forgotten, not all four.
    """
    d = tmp_path / "state-codex"
    d.mkdir()
    now = 1_700_000_000.0
    slot = d / "cx-1.state"

    slot.write_text('{"event":"PermissionRequest","t":%d}' % int(now - 3))
    states, _ = codex_state.scan(now, path=str(d), sweep=False)
    assert states == {"cx-1": base.STATE_WAITING}

    slot.write_text('{"event":"%s","t":%d}' % (clearing_event, int(now - 1)))
    states, _ = codex_state.scan(now, path=str(d), sweep=False)
    assert states == {"cx-1": expected}


def test_session_end_removes_the_slot_which_ends_the_wait_too(tmp_path):
    """The fifth way out: the terminal closed. The shim deletes the file, so
    there is nothing left to read -- asserted here because it is the one clear
    that happens outside derive_state."""
    d = tmp_path / "state-codex"
    d.mkdir()
    now = 1_700_000_000.0
    (d / "cx-1.state").write_text(
        '{"event":"PermissionRequest","t":%d}' % int(now - 3))
    assert codex_state.scan(now, path=str(d), sweep=False)[0]

    (d / "cx-1.state").unlink()
    assert codex_state.scan(now, path=str(d), sweep=False) == ({}, 0)


def test_a_missing_directory_is_an_ordinary_state(tmp_path):
    """Codex not installed, or the hook never registered. Both are normal."""
    assert codex_state.scan(1_700_000_000.0,
                            path=str(tmp_path / "nope"), sweep=False) == ({}, 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/pc/test_codex_state.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pc.providers.codex_state'`.

- [ ] **Step 3: Implement**

Create `pc/providers/codex_state.py`:

```python
"""Execution state for Codex sessions, out of Codex's own lifecycle hooks.

Codex's rollout log cannot answer "is this session waiting on a person". Its
approval events -- ExecApprovalRequest, ApplyPatchApprovalRequest,
RequestPermissions -- sit in the never-persisted arm of Codex's own persistence
policy, and four real rollouts on this desk contain none of them, including two
run with approval_policy "on-request". A session sitting on a prompt therefore
looks, in the file, exactly like a session nobody is using.

Its hooks can. Codex 0.150.0 ships a lifecycle hooks system whose event names
are the same words Claude Code uses and whose command hooks are fed the same
session_id and cwd on stdin, so `tools/blink-hook.sh` serves it unchanged --
with one argument telling it to write here instead of into ~/.blink/state.

Which is why this module is thin. The state machine, the slot format, the
abandonment sweep and the agent counting are all the same ones Claude's hooks
already needed, and they live in claude_state; sharing them is what keeps a
change to the machine from applying to only one of the two tools. What is NOT
shared is the directory, and that separation is the whole point: a Codex
session counted out of ~/.blink/state is reported to the board as a Claude one,
on a Claude pip, against a Claude account's limits.

The frames are not built here either. Codex already has a provider, and
pc/normalizer.select_pair shows two providers and drops a third -- so these
counts are unioned onto the existing codex frame in codex_cli.poll rather than
arriving as a provider of their own.
"""
import os

from pc.providers import claude_state

# Expanded when it is used, not here: a module-level expanduser is evaluated
# at import, before a test can move HOME, and the scan below DELETES files
# under this path. Same rule, and same reason, as claude_state.STATE_DIR.
STATE_DIR = "~/.blink/state-codex"


def scan(now_epoch, path=None, sweep=True):
    """({session_id: state}, live agents) for the Codex hook slots.

    The names the slots carry are dropped rather than returned. Naming the
    session on the panel is a separate feature with its own rule about when a
    name may be shown at all (claude_state.poll: only when exactly one session
    holds the winning state), and the Codex frame has no label story yet.
    Returning a value with no consumer would invite one to be wired up without
    that rule.
    """
    root = path if path is not None else os.path.expanduser(STATE_DIR)
    states, agents = claude_state.ClaudeStateProvider(
        path=root, sweep=sweep).session_states(now_epoch)
    return {sid: state for sid, (state, _name) in states.items()}, agents
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_codex_state.py -q`
Expected: PASS (9 tests, counting the parametrised four).

Run: `pytest tests -q`
Expected: 529 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/providers/codex_state.py tests/pc/test_codex_state.py
git commit -m "feat: read Codex execution state from its own hook slots"
```

---

### Task 6: A rollout file can name its own session

**Files:**
- Modify: `pc/providers/codex_cli.py` (add beside `_tail_lines`, around line 130)
- Test: `tests/pc/test_codex_cli.py`

**Interfaces:**
- Produces: `pc.providers.codex_cli.rollout_session_id(path: str) -> str` — the session id from the file's first line, or `""` for anything it cannot read with confidence.
- Consumed by: Task 7.

- [ ] **Step 1: Write the failing test**

Append to `tests/pc/test_codex_cli.py`:

```python
def test_rollout_session_id_comes_from_the_meta_line(tmp_path):
    """Line 1 of every rollout is its session_meta record, and it carries the
    id the hooks also report. That id is the only thing the two state sources
    share, so it is what lets them describe one session instead of two."""
    p = tmp_path / "rollout-x.jsonl"
    p.write_text(
        '{"type":"session_meta","payload":{"session_id":"cx-1",'
        '"cwd":"/Users/k/Projects/Blink","cli_version":"0.150.0"}}\n'
        '{"type":"event_msg","payload":{"type":"task_started"}}\n')
    assert codex_cli.rollout_session_id(str(p)) == "cx-1"


def test_rollout_session_id_reads_only_the_head(tmp_path):
    """A real rollout on this desk is 51 MB, and the tail reader would not
    reach line 1 at all. This asserts the head read does not depend on the
    rest of the file being small -- or even parseable."""
    p = tmp_path / "rollout-big.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"type":"session_meta","payload":{"session_id":"cx-2"}}\n')
        f.write("x" * (codex_cli.HEAD_BYTES * 4) + "\n")
    assert codex_cli.rollout_session_id(str(p)) == "cx-2"


@pytest.mark.parametrize("first_line", [
    '{"type":"event_msg","payload":{"type":"task_started"}}',   # not meta
    '{"type":"session_meta","payload":{}}',                     # meta, no id
    '{"type":"session_meta","payload":{"session_id":7}}',       # id not a string
    '{"type":"session_meta","payload":[]}',                     # payload not an object
    'not json at all',
])
def test_rollout_session_id_refuses_rather_than_guesses(tmp_path, first_line):
    """An empty string, never a guess. The caller treats "" as "this session
    cannot be matched to a hook slot", which is the safe answer -- it counts
    once from the rollout and is never merged with the wrong slot."""
    p = tmp_path / "rollout-odd.jsonl"
    p.write_text(first_line + "\n")
    assert codex_cli.rollout_session_id(str(p)) == ""


def test_rollout_session_id_refuses_an_unterminated_first_line(tmp_path):
    """A first line longer than the bound is not a rollout we understand."""
    p = tmp_path / "rollout-long.jsonl"
    p.write_text("{" + "a" * (codex_cli.HEAD_BYTES + 10))
    assert codex_cli.rollout_session_id(str(p)) == ""


def test_rollout_session_id_on_a_missing_file_is_empty(tmp_path):
    assert codex_cli.rollout_session_id(str(tmp_path / "nope.jsonl")) == ""
```

If `pytest` is not already imported at the top of `tests/pc/test_codex_cli.py`, add `import pytest` there.

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/pc/test_codex_cli.py -q -k rollout_session_id`
Expected: FAIL with `AttributeError: module 'pc.providers.codex_cli' has no attribute 'rollout_session_id'`.

- [ ] **Step 3: Implement**

In `pc/providers/codex_cli.py`, immediately after `_tail_lines` (and after the `TAIL_BYTES` constant, near it), add:

```python
# The session_meta record is line 1 of every rollout and it is small. The
# bound is what stops a file whose first line is enormous -- which is not a
# rollout we understand -- from being read into memory to find that out.
HEAD_BYTES = 8192


def rollout_session_id(path: str) -> str:
    """The session id from a rollout's first line, or "".

    Line 1 of every rollout is its `session_meta` record and it carries the
    same session id Codex hands to a hook on stdin. That id is the only thing
    the two state sources have in common, and without it a session both of
    them can see would be counted twice: the pip row would say two where a
    person can see one terminal.

    A HEAD read, not the tail one parse_rollout_tail already does. The meta
    line is at the START of the file and a real rollout on this desk is 51 MB,
    so the 256 KB tail will usually not contain it at all.

    Every failure returns "". The caller treats that as "this session cannot
    be matched to a hook slot" -- it still counts once, from the rollout, and
    is never merged with the wrong slot. A wrong id would be far worse than no
    id, so nothing here is inferred.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
    except OSError:
        return ""
    line, sep, _rest = head.partition(b"\n")
    if not sep:
        # No newline inside the bound: either an empty file or a first line
        # bigger than any session_meta record has ever been. Refuse both.
        return ""
    try:
        rec = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return ""
    if not isinstance(rec, dict) or rec.get("type") != "session_meta":
        return ""
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return ""
    sid = payload.get("session_id")
    return sid if isinstance(sid, str) else ""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_codex_cli.py -q`
Expected: PASS.

Run: `pytest tests -q`
Expected: 538 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/providers/codex_cli.py tests/pc/test_codex_cli.py
git commit -m "feat: read a rollout's session id from its meta line"
```

---

### Task 7: The Codex frame unions hook state over rollout state

Two sources now describe the same Codex sessions. They must produce ONE state frame carrying ONE census, or the pip row double-counts. The hook wins per session: it is the only one of the two that can see a permission prompt at all, so a rollout saying `running` for a session whose hook said `waiting` is not a disagreement, it is the older and blinder answer.

**Files:**
- Modify: `pc/providers/codex_cli.py` (`CodexCliProvider.__init__`, `CodexCliProvider.poll`)
- Test: `tests/pc/test_codex_cli.py`

**Interfaces:**
- Consumes: `codex_state.scan(now_epoch, path, sweep)` (Task 5); `rollout_session_id(path)` (Task 6).
- Produces: `CodexCliProvider(root=None, state_dir=None, sweep=True)`; `poll()` still returns at most two frames — the usage frame (`src="cli"`) and a single state frame (`src="cli-state"`) whose counts now include `n_wait` and `n_agents`.

- [ ] **Step 1: Write the failing test**

Append to `tests/pc/test_codex_cli.py`:

```python
def _rollout(dirpath, sid, event_type, t):
    """One rollout file with a meta line and one turn event."""
    import os as _os
    leaf = _os.path.join(str(dirpath), "2026", "09", "03")
    _os.makedirs(leaf, exist_ok=True)
    p = _os.path.join(leaf, "rollout-%s.jsonl" % sid)
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"type":"session_meta","payload":{"session_id":"%s"}}\n' % sid)
        f.write('{"type":"event_msg","timestamp":%d,'
                '"payload":{"type":"%s"}}\n' % (int(t), event_type))
    return p


def test_a_waiting_hook_slot_beats_a_running_rollout(tmp_path):
    """The rollout cannot see a permission prompt -- Codex never persists the
    approval events -- so `running` from it is the older, blinder answer for a
    session whose hook has since said `waiting`."""
    sessions = tmp_path / "sessions"
    slots = tmp_path / "state-codex"
    slots.mkdir()
    now = 1_700_000_000.0
    _rollout(sessions, "cx-1", "task_started", now - 30)
    (slots / "cx-1.state").write_text(
        '{"event":"PermissionRequest","t":%d}' % int(now - 5))

    frames = codex_cli.CodexCliProvider(
        root=str(sessions), state_dir=str(slots), sweep=False).poll(now)

    state_frames = [f for f in frames if f.src == codex_cli.STATE_SRC_ID]
    assert len(state_frames) == 1, "one census, or the pip row double-counts"
    f = state_frames[0]
    assert f.provider == "codex"
    assert f.state == base.STATE_WAITING
    assert (f.n_run, f.n_wait, f.n_idle) == (0, 1, 0)


def test_one_session_seen_by_both_sources_is_counted_once(tmp_path):
    """The failure this whole union exists to prevent."""
    sessions = tmp_path / "sessions"
    slots = tmp_path / "state-codex"
    slots.mkdir()
    now = 1_700_000_000.0
    _rollout(sessions, "cx-1", "task_started", now - 30)
    (slots / "cx-1.state").write_text(
        '{"event":"PreToolUse","t":%d}' % int(now - 5))

    frames = codex_cli.CodexCliProvider(
        root=str(sessions), state_dir=str(slots), sweep=False).poll(now)
    f = [x for x in frames if x.src == codex_cli.STATE_SRC_ID][0]

    assert (f.n_run, f.n_wait, f.n_idle) == (1, 0, 0)


def test_a_session_only_the_rollout_can_see_still_counts(tmp_path):
    """A Codex session that was already open when the hook was installed has
    no slot and never will. Dropping it would make a running terminal vanish
    from the panel, which is worse than not knowing it is waiting."""
    sessions = tmp_path / "sessions"
    slots = tmp_path / "state-codex"
    slots.mkdir()
    now = 1_700_000_000.0
    _rollout(sessions, "old-1", "task_started", now - 30)
    (slots / "cx-2.state").write_text(
        '{"event":"PermissionRequest","t":%d}' % int(now - 5))

    frames = codex_cli.CodexCliProvider(
        root=str(sessions), state_dir=str(slots), sweep=False).poll(now)
    f = [x for x in frames if x.src == codex_cli.STATE_SRC_ID][0]

    assert (f.n_run, f.n_wait) == (1, 1)
    assert f.state == base.STATE_WAITING, "waiting outranks running"


def test_a_rollout_with_no_readable_id_still_counts_once(tmp_path):
    """The degradation path for Task 6's refusals: an unidentifiable rollout
    is keyed by its own path, so it can never collide with a hook slot and can
    never be merged into the wrong one."""
    import os as _os
    sessions = tmp_path / "sessions"
    leaf = sessions / "2026" / "09" / "03"
    leaf.mkdir(parents=True)
    now = 1_700_000_000.0
    p = leaf / "rollout-anon.jsonl"
    p.write_text('not json at all\n'
                 '{"type":"event_msg","timestamp":%d,'
                 '"payload":{"type":"task_started"}}\n' % int(now - 30))
    slots = tmp_path / "state-codex"
    slots.mkdir()

    frames = codex_cli.CodexCliProvider(
        root=str(sessions), state_dir=str(slots), sweep=False).poll(now)
    f = [x for x in frames if x.src == codex_cli.STATE_SRC_ID][0]

    assert f.n_run == 1
    assert _os.path.basename(p)  # the file was the key; nothing else asserts it


def test_hook_slots_alone_produce_a_frame(tmp_path):
    """Codex hooks installed, no rollout old enough to be in the recent set.
    The census still has to reach the board."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    slots = tmp_path / "state-codex"
    slots.mkdir()
    now = 1_700_000_000.0
    (slots / "cx-1.state").write_text(
        '{"event":"PermissionRequest","t":%d}' % int(now - 5))

    frames = codex_cli.CodexCliProvider(
        root=str(sessions), state_dir=str(slots), sweep=False).poll(now)

    assert [f.state for f in frames] == [base.STATE_WAITING]
    assert frames[0].session_pct == base.UNKNOWN, \
        "a state frame carries no percentage and must never win the dial"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/pc/test_codex_cli.py -q -k "waiting_hook_slot or counted_once or only_the_rollout or readable_id or slots_alone"`
Expected: FAIL with `TypeError: CodexCliProvider.__init__() got an unexpected keyword argument 'state_dir'`.

- [ ] **Step 3: Implement**

In `pc/providers/codex_cli.py`, add the import beside the existing one from `claude_state`:

```python
from pc.providers import codex_state  # noqa: E402
```

Replace `CodexCliProvider.__init__`:

```python
    def __init__(self, root=None, state_dir=None, sweep=True):
        self._root = root
        # The hook slot directory, injectable for the same reason `root` is:
        # a test must never be able to reach the real one, because the scan
        # behind it deletes abandoned files.
        self._state_dir = state_dir
        self._sweep = sweep
```

Replace the tail of `poll()` — from `state = parse_rollout_state(lines, now_epoch)` to the `return frames` — with:

```python
            state = parse_rollout_state(lines, now_epoch)
            if state != base.STATE_UNKNOWN:
                # Keyed by session id so a hook slot below can OVERRIDE this
                # session rather than be counted beside it. A rollout whose
                # meta line could not be read falls back to its own path as
                # the key: it still counts once, it simply cannot be matched
                # to a slot -- and a path can never collide with a session id.
                rollout_states[rollout_session_id(path) or path] = state
            limits, observed_at = parse_rollout_tail(lines, mtime)
            if limits is None:
                continue
            frame = self.parse_cli_event(limits, now_epoch, observed_at)
            if frame is None:
                continue
            if best is None or frame.observed_at > best.observed_at:
                best = frame
        frames = [best] if best is not None else []

        # The hooks' answer, and where it wins.
        #
        # The rollout cannot see a permission prompt at all: Codex files its
        # approval events in the never-persisted arm of its own policy, and
        # four real rollouts on this desk contain none of them. So `running`
        # from the rollout for a session whose hook said `waiting` is not a
        # disagreement to be resolved on recency -- it is the older and
        # blinder of two answers, and the newer one simply replaces it.
        #
        # Unioned rather than substituted, though: a Codex session that was
        # already open when the hook was installed has no slot and never will.
        # Dropping it would make a running terminal disappear from the panel,
        # which is a worse error than not knowing it is waiting.
        hook_states, agents = codex_state.scan(
            now_epoch, path=self._state_dir, sweep=self._sweep)
        merged = dict(rollout_states)
        merged.update(hook_states)

        counts = {}
        for state in merged.values():
            counts[state] = counts.get(state, 0) + 1

        if counts:
            # A separate frame with no percentages, exactly as Claude's state
            # provider does it: it can never win a recency contest for
            # numbers, and the normalizer merges its state field by field.
            frames.append(base.NormalizedUsageFrame(
                provider=PROVIDER_ID,
                src=STATE_SRC_ID,
                observed_at=now_epoch,
                state=base.worst_of(counts),
                n_run=counts.get(base.STATE_RUNNING, 0),
                # n_wait was absent while nothing could produce a waiting
                # Codex session. The hooks can, and pc/protocol._pair_from
                # reads this field to decide what the panel's line says --
                # so leaving it out would have lit an amber pip beside the
                # words "0 sessions".
                n_wait=counts.get(base.STATE_WAITING, 0),
                n_idle=counts.get(base.STATE_IDLE, 0),
                n_stuck=(counts.get(base.STATE_STUCK, 0)
                         + counts.get(base.STATE_FAILED, 0)),
                n_agents=agents,
            ))
        return frames
```

and, at the top of `poll()`, replace `counts = {}` with:

```python
        rollout_states = {}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_codex_cli.py -q`
Expected: PASS.

Run: `pytest tests/pc/test_ingest.py tests/pc/test_normalizer.py tests/pc/test_protocol.py -q`
Expected: PASS — this is the check that the summed pip row is still right.

Run: `pytest tests -q`
Expected: 543 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/providers/codex_cli.py tests/pc/test_codex_cli.py
git commit -m "feat: Codex hook slots and rollouts describe one census"
```

---

### Task 8: Register the shim in Codex's hooks file

**Files:**
- Create: `pc/install_codex_hooks.py`
- Test: `tests/pc/test_install_codex_hooks.py` (create)

**Interfaces:**
- Consumes: Task 1 findings **F1** (path), **F2** (shape), **F4** (matcher group). Task 2's shim argument.
- Produces: `codex_home()`, `hooks_file()`, `HOOK_EVENTS`, `hook_command(shim_path, event)`, `install(hooks_path, shim_path) -> str`. `uninstall` arrives in Task 9.

**Before writing code, apply Task 1's findings:**
- `_HOOKS_PATH_PARTS` below is **`("hooks.json",)`** — CORRECTED 2026-09-04. The plan was written against `("hooks", "hooks.json")` and **that path does not work**: F1 verified by execution that `$CODEX_HOME/hooks.json` fires and `$CODEX_HOME/hooks/hooks.json` is silently ignored, with a no-file control proving the negative is real. The `hooks/hooks.json` string in the binary belongs to the PLUGIN loader, not the user layer. Do not change it back.
- `_EVENTS_KEY` below is `"hooks"`, for a file shaped `{"hooks": {"PreToolUse": [...]}}`. If **F2** recorded the events at the top level instead, set `_EVENTS_KEY = None`. Both branches are implemented and both are tested; this is a one-constant switch, not a rewrite.
- The matcher group is written as `{"matcher": "*", "hooks": [{"type": "command", "command": "..."}]}`. If **F4** recorded different key names, change `_entries_for` and `install` together and update the test's expected JSON.

- [ ] **Step 1: Write the failing test**

Create `tests/pc/test_install_codex_hooks.py`:

```python
"""Registering Blink's shim with Codex, and never damaging its config."""
import json

import pytest

from pc import install_codex_hooks as ich
from pc.install_statusline import SettingsUnreadable


def _events(data):
    return data["hooks"] if ich._EVENTS_KEY else data


def test_the_command_carries_the_codex_argument():
    """Without it the shim writes Codex sessions into ~/.blink/state and the
    board reports them as Claude ones."""
    cmd = ich.hook_command("/home/k/.blink/blink-hook.sh", "PreToolUse")
    assert cmd.endswith("PreToolUse codex")


def test_install_writes_one_group_per_event(tmp_path):
    p = tmp_path / "hooks.json"
    ich.install(str(p), "/home/k/.blink/blink-hook.sh")

    data = json.loads(p.read_text(encoding="utf-8"))
    events = _events(data)
    assert set(events) == {ev for ev, _ in ich.HOOK_EVENTS}
    group = events["PreToolUse"][0]
    assert group["matcher"] == "*"
    assert group["hooks"] == [{
        "type": "command",
        "command": "sh /home/k/.blink/blink-hook.sh PreToolUse codex"}]
    assert "matcher" not in events["Stop"][0], \
        "events that take no matcher must not be given one"


def test_the_waiting_event_and_all_of_its_clears_are_registered():
    """A waiting state with no way out is worse than no waiting state. If
    PermissionRequest is registered, everything that can follow it must be
    too -- otherwise the amber pip never goes back."""
    events = {ev for ev, _ in ich.HOOK_EVENTS}
    assert "PermissionRequest" in events
    for clearing in ("PostToolUse", "Stop", "Interrupt", "UserPromptSubmit",
                     "SessionEnd"):
        assert clearing in events, f"{clearing} cannot clear a wait it never sees"


def test_install_is_idempotent(tmp_path):
    """A reinstall must not stack a second copy that then fires twice per
    tool call forever."""
    p = tmp_path / "hooks.json"
    shim = "/home/k/.blink/blink-hook.sh"
    ich.install(str(p), shim)
    msg = ich.install(str(p), shim)

    events = _events(json.loads(p.read_text(encoding="utf-8")))
    assert len(events["PreToolUse"]) == 1
    assert len(events["PreToolUse"][0]["hooks"]) == 1
    assert "already installed" in msg


def test_install_repoints_a_moved_shim(tmp_path):
    """What `blink update` does every time it moves the binary. Without this
    the old entries are orphaned: invisible to uninstall, still invoking a
    script that is not there, and a third install appends a duplicate."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), "/old/blink-hook.sh")
    msg = ich.install(str(p), "/new/blink-hook.sh")

    events = _events(json.loads(p.read_text(encoding="utf-8")))
    assert len(events["PreToolUse"]) == 1
    assert events["PreToolUse"][0]["hooks"][0]["command"] == \
        "sh /new/blink-hook.sh PreToolUse codex"
    assert "repointed" in msg


def test_install_never_touches_someone_elses_hook(tmp_path):
    p = tmp_path / "hooks.json"
    theirs = {"matcher": "*", "hooks": [
        {"type": "command", "command": "/usr/local/bin/audit.sh"}]}
    payload = ({"hooks": {"PreToolUse": [theirs]}} if ich._EVENTS_KEY
               else {"PreToolUse": [theirs]})
    p.write_text(json.dumps(payload), encoding="utf-8")

    ich.install(str(p), "/home/k/.blink/blink-hook.sh")

    events = _events(json.loads(p.read_text(encoding="utf-8")))
    commands = [h["command"] for g in events["PreToolUse"] for h in g["hooks"]]
    assert "/usr/local/bin/audit.sh" in commands


def test_install_refuses_a_file_it_cannot_parse(tmp_path):
    """The judgement call, stated: refuse and change nothing.

    A hooks file that does not parse is usually a file someone is halfway
    through editing, and it belongs to another vendor's tool. Repairing it
    means writing our idea of it over theirs. install_statusline has refused
    on this exact ground since it was written; this follows it.
    """
    p = tmp_path / "hooks.json"
    before = '{"hooks": {"PreToolUse": [oops'
    p.write_text(before, encoding="utf-8")

    with pytest.raises(SettingsUnreadable):
        ich.install(str(p), "/home/k/.blink/blink-hook.sh")
    assert p.read_text(encoding="utf-8") == before, \
        "an unparseable file must come out byte-identical"


def test_install_refuses_a_hooks_key_that_is_not_an_object(tmp_path):
    p = tmp_path / "hooks.json"
    payload = ({"hooks": []} if ich._EVENTS_KEY else {"PreToolUse": "nope"})
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SettingsUnreadable):
        ich.install(str(p), "/home/k/.blink/blink-hook.sh")


def test_the_marker_records_what_was_written(tmp_path, monkeypatch):
    """Uninstall matches on the marker rather than on the command text, so a
    customer hook that merely mentions our filename is never deleted."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), "/home/k/.blink/blink-hook.sh")
    recorded = ich._read_marker()
    assert "sh /home/k/.blink/blink-hook.sh Stop codex" in recorded


def test_codex_home_honours_the_environment(monkeypatch, tmp_path):
    """Codex itself honours CODEX_HOME, and codex_cli.sessions_root already
    does. Writing to ~/.codex on a machine that redirects it would register a
    hook nothing ever reads."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    assert ich.codex_home() == str(tmp_path / "elsewhere")
    assert ich.hooks_file().startswith(str(tmp_path / "elsewhere"))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/pc/test_install_codex_hooks.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pc.install_codex_hooks'`.

- [ ] **Step 3: Implement**

Create `pc/install_codex_hooks.py`:

```python
"""Register Blink's hook shim with Codex's own lifecycle hooks.

Same two rules as pc/install_hooks and pc/install_statusline: never lose
anything the user put there, and never rewrite a key that is not ours. What
differs is whose file it is. ~/.claude/settings.json is edited by a product we
integrate with deliberately; Codex's hooks file belongs to a different vendor
and is documented as a place people write their own automation. So the refusal
path here is not a fallback -- it is the expected behaviour whenever the file
is anything but exactly what we understand.

The shape below is pinned in docs/research/codex-hook-contract.md against
codex-cli 0.150.0. Two constants carry the parts most likely to move:
_HOOKS_PATH_PARTS and _EVENTS_KEY. Re-run that document's probes after a Codex
upgrade rather than discovering a change from a support ticket.

TRUST: Codex requires persisted trust for hook sources and prompts once in its
TUI, recording a `trusted_hash` of the hooks file. Nothing here can answer that
prompt and nothing here should try -- `blink install` says it is coming
(pc/cli.cmd_install) and the person answers it. A changed hooks file
invalidates the hash, so a `blink update` that moves the shim will prompt
again; that is Codex working as designed, not a fault.
"""
import json
import os
import shlex
import sys

from pc.install_statusline import (SettingsUnreadable, _load, _save,
                                   _sniff_format, windows_bash_path)

INSTALLED_MARKER_PATH = "~/.blink/codex-hooks-installed-commands"

# F1: where Codex reads its hooks file from, under CODEX_HOME.
_HOOKS_PATH_PARTS = ("hooks.json",)

# F2: the key the event map hangs off, or None when the events are the
# top-level object. One constant because it is the single thing about this
# file's shape that the strings in the binary could not settle on their own.
_EVENTS_KEY = "hooks"

# The lifecycle events we register, each with the matcher its group is written
# with (None for events that take none).
#
# PermissionRequest is the point of the whole exercise: it is the event Codex
# fires when it is blocked on a person, and it is the one thing its rollout log
# provably cannot tell us (the approval events sit in the never-persisted arm
# of Codex's own policy). Everything else on this list is here either to say a
# session is alive or to CLEAR that prompt -- PostToolUse when the tool was
# approved and ran, UserPromptSubmit when the person typed something else, Stop
# when the turn finished, Interrupt when they pressed Esc, SessionEnd when the
# terminal closed. A waiting state with no way out is worse than no waiting
# state at all, so none of those five may be dropped to save a hook call.
#
# Not registered: PreCompact and PostCompact (they say "still running", which
# the tool events already say) and Codex's Prompt/McpTool/Agent handler kinds
# (we run a command).
HOOK_EVENTS = (
    ("SessionStart", None),
    ("UserPromptSubmit", None),
    ("PreToolUse", "*"),
    ("PostToolUse", "*"),
    ("PermissionRequest", "*"),
    ("Stop", None),
    ("Interrupt", None),
    ("SessionEnd", None),
    # Subagent lifetimes. Whether Codex's payload carries an agent_id is
    # unverified; if it does not, the shim's fallback puts every agent of a
    # session in one file and the count reads 1 instead of N. An undercount,
    # never a crash -- see Task 14 in docs/plans/codex-hook-shim.md.
    ("SubagentStart", "*"),
    ("SubagentStop", "*"),
)


def codex_home() -> str:
    """CODEX_HOME or ~/.codex, exactly as pc/providers/codex_cli does it."""
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def hooks_file() -> str:
    return os.path.join(codex_home(), *_HOOKS_PATH_PARTS)


def hook_command(shim_path: str, event: str) -> str:
    """The exact string to write as a hook command.

    `bash` and forward slashes on Windows for the reason
    install_statusline.windows_bash_path spells out at length: a non-ASCII home
    directory does not survive the hand-over to Git Bash.

    The trailing `codex` is what sends the slots to ~/.blink/state-codex. It is
    the whole difference between this registration and the Claude one, and
    without it every Codex session on the machine is reported to the board as a
    Claude session against a Claude account's limits.
    """
    if sys.platform == "win32":
        return f"bash {windows_bash_path(shim_path)} {event} codex"
    return f"sh {shlex.quote(shim_path)} {event} codex"


def _marker_path():
    return os.path.expanduser(INSTALLED_MARKER_PATH)


def _read_marker() -> set:
    try:
        with open(_marker_path(), encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def _write_marker(commands) -> None:
    os.makedirs(os.path.dirname(_marker_path()), exist_ok=True)
    # encoding= because the commands carry the shim's path, which carries the
    # user's home directory -- and a Windows locale cannot encode every name a
    # home directory can have.
    with open(_marker_path(), "w", encoding="utf-8") as f:
        for c in sorted(commands):
            f.write(c + "\n")


def _remove_marker() -> None:
    try:
        os.remove(_marker_path())
    except OSError:
        pass


def _ours(command: str, expected: set, marker: set) -> bool:
    """Ours if the marker recorded it, or if it is what we would write now.

    Both checks: the marker survives a shim path that has since changed, and
    the computed form survives a marker file lost with the rest of ~/.blink.
    """
    return command in marker or command in expected


def _event_map(data):
    """The object holding the per-event lists, created if absent.

    Never replaced. Whatever is in there belongs to the user or to another
    tool, and a hooks file whose event map is not an object is a file we do
    not understand -- which is the one situation where doing nothing is the
    only safe move.
    """
    if _EVENTS_KEY is None:
        return data
    events = data.setdefault(_EVENTS_KEY, {})
    if not isinstance(events, dict):
        raise SettingsUnreadable(f"'{_EVENTS_KEY}' is not an object")
    return events


def _entries_for(data, event):
    """The matcher-group list for one event, created if absent."""
    events = _event_map(data)
    lst = events.setdefault(event, [])
    if not isinstance(lst, list):
        raise SettingsUnreadable(f"{event} is not a list")
    return lst


def install(hooks_path: str, shim_path: str) -> str:
    """Add our hook to each lifecycle event. Idempotent.

    Raises SettingsUnreadable, and changes nothing, when the file is there and
    cannot be parsed or is not shaped the way we understand. The caller
    reports that and carries on: the activity light is a nicety, and someone
    else's config is not ours to repair.
    """
    indent, trailing_newline = _sniff_format(hooks_path)
    data = _load(hooks_path)

    expected = {hook_command(shim_path, ev) for ev, _ in HOOK_EVENTS}
    marker = _read_marker()
    added = 0
    repointed = 0
    for event, matcher in HOOK_EVENTS:
        command = hook_command(shim_path, event)
        entries = _entries_for(data, event)

        # Already present, in any group -- a reinstall must not stack a second
        # copy that then fires twice per tool call forever. But "present" is
        # not "correct": an entry that matches only via the MARKER names an
        # older shim path, which is what `blink update` produces every time it
        # moves the binary. Left as-is those entries are orphaned instantly,
        # invisible to uninstall, and a third install appends a duplicate.
        ours = ours_group = None
        for group in entries:
            if not isinstance(group, dict):
                continue
            for h in (group.get("hooks") or []):
                if isinstance(h, dict) and _ours(h.get("command", ""),
                                                 expected, marker):
                    ours, ours_group = h, group
                    break
            if ours is not None:
                break

        if ours is not None:
            if ours.get("command") != command:
                ours["command"] = command
                repointed += 1
            # The matcher is ours to correct too, but only when the group
            # holds our hook ALONE -- rewriting a matcher on a group we share
            # with someone else would change when their hook fires.
            if (len(ours_group.get("hooks") or []) == 1
                    and ours_group.get("matcher") != matcher):
                if matcher:
                    ours_group["matcher"] = matcher
                else:
                    ours_group.pop("matcher", None)
                repointed += 1
            continue

        group = {"hooks": [{"type": "command", "command": command}]}
        if matcher:
            group["matcher"] = matcher
        entries.append(group)
        added += 1

    _save(hooks_path, data, indent, trailing_newline)
    _write_marker(expected)
    if added == 0 and repointed == 0:
        return "Codex state hooks already installed."
    if added == 0:
        return f"Codex state hooks repointed at the new path ({repointed})."
    if repointed:
        return (f"Codex state hooks installed ({added} events,"
                f" {repointed} repointed).")
    return f"Codex state hooks installed ({added} events)."
```

Note: `json` is imported for the module's docstring contract and for symmetry with `install_hooks`; if `flake8` flags it as unused after Task 9 as well, remove the import in Task 9's commit rather than leaving a lint failure.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_install_codex_hooks.py -q`
Expected: PASS.

Run: `pytest tests -q`
Expected: 554 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/install_codex_hooks.py tests/pc/test_install_codex_hooks.py
git commit -m "feat: register the Blink shim with Codex's lifecycle hooks"
```

---

### Task 9: Unregister it just as cleanly

**Files:**
- Modify: `pc/install_codex_hooks.py`
- Test: `tests/pc/test_install_codex_hooks.py`

**Interfaces:**
- Produces: `install_codex_hooks.uninstall(hooks_path: str, shim_path: str = None) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/pc/test_install_codex_hooks.py`:

```python
def test_uninstall_returns_the_file_to_the_shape_it_had(tmp_path):
    p = tmp_path / "hooks.json"
    shim = "/home/k/.blink/blink-hook.sh"
    ich.install(str(p), shim)
    msg = ich.uninstall(str(p), shim)

    assert json.loads(p.read_text(encoding="utf-8")) == {}
    assert "removed" in msg
    assert ich._read_marker() == set()


def test_uninstall_keeps_someone_elses_hook(tmp_path):
    p = tmp_path / "hooks.json"
    shim = "/home/k/.blink/blink-hook.sh"
    ich.install(str(p), shim)
    data = json.loads(p.read_text(encoding="utf-8"))
    _events(data)["PreToolUse"].append(
        {"matcher": "*", "hooks": [
            {"type": "command", "command": "/usr/local/bin/audit.sh"}]})
    p.write_text(json.dumps(data), encoding="utf-8")

    ich.uninstall(str(p), shim)

    events = _events(json.loads(p.read_text(encoding="utf-8")))
    assert [h["command"] for g in events["PreToolUse"] for h in g["hooks"]] \
        == ["/usr/local/bin/audit.sh"]


def test_uninstall_removes_a_moved_shim_by_its_marker(tmp_path):
    """The entries `blink update` left behind name an old path. The marker is
    the only thing that still proves they are ours."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), "/old/blink-hook.sh")
    ich.uninstall(str(p), "/new/blink-hook.sh")
    assert json.loads(p.read_text(encoding="utf-8")) == {}


def test_uninstall_leaves_an_unparseable_file_alone(tmp_path):
    p = tmp_path / "hooks.json"
    before = "{oops"
    p.write_text(before, encoding="utf-8")
    msg = ich.uninstall(str(p), "/home/k/.blink/blink-hook.sh")
    assert p.read_text(encoding="utf-8") == before
    assert "left it alone" in msg


def test_uninstall_with_no_hooks_file_is_not_an_error(tmp_path):
    msg = ich.uninstall(str(tmp_path / "nope.json"),
                        "/home/k/.blink/blink-hook.sh")
    assert "No Codex state hooks" in msg
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/pc/test_install_codex_hooks.py -q -k uninstall`
Expected: FAIL with `AttributeError: module 'pc.install_codex_hooks' has no attribute 'uninstall'`.

- [ ] **Step 3: Implement**

Append to `pc/install_codex_hooks.py`:

```python
def uninstall(hooks_path: str, shim_path: str = None) -> str:
    """Remove only our entries, and only the ones we can prove are ours.

    Symmetric with install by construction: it drops exactly the commands the
    marker recorded plus the ones this call would itself write, and leaves
    every other entry in place. An empty group left behind by that removal is
    dropped too, and an event whose list ends up empty loses its key -- so a
    machine that has uninstalled has a hooks file shaped the way it was before
    Blink ever ran, rather than a skeleton of empty lists.
    """
    try:
        data = _load(hooks_path)
    except SettingsUnreadable as e:
        # Never "repair" a file we cannot parse by writing a fresh one over
        # it. It is someone's config, probably mid-edit.
        return f"Codex hooks file could not be read ({e}); left it alone."

    expected = ({hook_command(shim_path, ev) for ev, _ in HOOK_EVENTS}
                if shim_path else set())
    marker = _read_marker()
    try:
        events = _event_map(data)
    except SettingsUnreadable:
        _remove_marker()
        return "No Codex state hooks to remove."

    removed = 0
    for event, _ in HOOK_EVENTS:
        entries = events.get(event)
        if not isinstance(entries, list):
            continue
        kept_groups = []
        for group in entries:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                kept_groups.append(group)
                continue
            kept = [h for h in inner
                    if not (isinstance(h, dict)
                            and _ours(h.get("command", ""), expected, marker))]
            removed += len(inner) - len(kept)
            if kept:
                group["hooks"] = kept
                kept_groups.append(group)
            # A group whose only hook was ours is dropped entirely rather than
            # left as an empty shell with a dangling matcher.
        if kept_groups:
            events[event] = kept_groups
        else:
            events.pop(event, None)

    if _EVENTS_KEY is not None and not events:
        data.pop(_EVENTS_KEY, None)

    indent, trailing_newline = _sniff_format(hooks_path)
    _save(hooks_path, data, indent, trailing_newline)
    _remove_marker()
    if removed == 0:
        return "No Codex state hooks to remove."
    return f"Codex state hooks removed ({removed})."
```

Remove the now-unused `import json` from the top of the module if `flake8 pc/install_codex_hooks.py` reports F401.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_install_codex_hooks.py -q`
Expected: PASS.

Run: `flake8 pc/install_codex_hooks.py`
Expected: no output.

Run: `pytest tests -q`
Expected: 559 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/install_codex_hooks.py tests/pc/test_install_codex_hooks.py
git commit -m "feat: uninstall the Codex hooks as cleanly as the Claude ones"
```

---

### Task 10: The `config.toml` pointer — only if Task 1 finding F3 says it is needed

**SKIP THIS TASK. F3 answered it: no pointer key is needed.** Verified by execution on 2026-09-04 — a sandbox `config.toml` with no hooks-related key at all still fired the hook, because `hooks.json` is discovered purely by location. `config.toml` IS still written by `blink install`, but for the **trust record** (F5, `[hooks.state."<key>"]`), which belongs to Task 8, not to this task's `install_pointer`/`remove_pointer`. Note the skip in the report and go to Task 11.

~~Run this task only if F3 recorded that `~/.codex/config.toml` needs a key pointing at the hooks file.~~ If F3 recorded that Codex reads the default path with no key, skip it, note the skip in your report, and go to Task 11.

There is no TOML library in this project and adding one is a dependency decision nobody has made, so this does not parse TOML. It appends a marker-bounded block at end of file, which is valid TOML by construction (a `[table]` header starts a new table and cannot be captured by anything above it) and is removable exactly. It refuses whenever a `hooks` table already exists, because a duplicate table header is a TOML parse error and would break Codex outright.

**Files:**
- Modify: `pc/install_codex_hooks.py`
- Test: `tests/pc/test_install_codex_hooks.py`

**Interfaces:**
- Produces: `config_file()`, `install_pointer(config_path: str) -> str`, `remove_pointer(config_path: str) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/pc/test_install_codex_hooks.py`:

```python
BEFORE = '[projects."/Users/k/Projects"]\ntrust_level = "trusted"\n'


def test_the_pointer_is_appended_and_nothing_above_it_moves(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(BEFORE, encoding="utf-8")

    ich.install_pointer(str(p))

    text = p.read_text(encoding="utf-8")
    assert text.startswith(BEFORE), "the user's own config must be untouched"
    assert ich._POINTER_BEGIN in text and ich._POINTER_END in text


def test_the_pointer_is_removed_exactly(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(BEFORE, encoding="utf-8")
    ich.install_pointer(str(p))
    ich.remove_pointer(str(p))
    assert p.read_text(encoding="utf-8") == BEFORE


def test_installing_the_pointer_twice_leaves_one_block(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(BEFORE, encoding="utf-8")
    ich.install_pointer(str(p))
    ich.install_pointer(str(p))
    assert p.read_text(encoding="utf-8").count(ich._POINTER_BEGIN) == 1


def test_a_hand_written_hooks_table_is_refused_not_duplicated(tmp_path):
    """A second [hooks] header is a TOML parse error, and a config.toml that
    does not parse is a Codex that does not start. Refuse and change nothing."""
    p = tmp_path / "config.toml"
    before = BEFORE + '\n[hooks]\nmanaged_dir = "/opt/hooks"\n'
    p.write_text(before, encoding="utf-8")

    with pytest.raises(SettingsUnreadable):
        ich.install_pointer(str(p))
    assert p.read_text(encoding="utf-8") == before


def test_a_missing_config_gets_one_with_only_our_block(tmp_path):
    p = tmp_path / "config.toml"
    ich.install_pointer(str(p))
    text = p.read_text(encoding="utf-8")
    assert text.startswith(ich._POINTER_BEGIN)


def test_removing_a_pointer_that_is_not_there_changes_nothing(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(BEFORE, encoding="utf-8")
    ich.remove_pointer(str(p))
    assert p.read_text(encoding="utf-8") == BEFORE
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/pc/test_install_codex_hooks.py -q -k pointer`
Expected: FAIL with `AttributeError: module 'pc.install_codex_hooks' has no attribute 'install_pointer'`.

- [ ] **Step 3: Implement**

Append to `pc/install_codex_hooks.py`, and set `_POINTER_BODY` to the exact key and value Task 1's F3 recorded:

```python
# The config.toml pointer, when Codex needs one to find the hooks file.
#
# Appended as a marker-bounded block rather than edited in place, because
# there is no TOML library in this project and adding one is a dependency
# decision nobody has made. A block at end of file is valid TOML by
# construction -- a [table] header starts a new table and cannot be captured
# by anything above it -- and it is removable to the byte, which an edited
# document would not be: every TOML round-tripper this project could take on
# reformats and drops comments, and this is a file people hand-edit.
_POINTER_BEGIN = "# >>> blink hooks pointer >>>"
_POINTER_END = "# <<< blink hooks pointer <<<"
# F3 from docs/research/codex-hook-contract.md.
_POINTER_BODY = '[hooks]\nhooks = "hooks.json"'   # DEAD: F3 says no pointer is needed


def config_file() -> str:
    return os.path.join(codex_home(), "config.toml")


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as e:
        raise SettingsUnreadable(f"{path} could not be read ({e})")


def _write_text(path, text):
    """Via a temp sibling, so a crash cannot truncate the user's config."""
    if os.path.islink(path):
        path = os.path.realpath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".blink-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _strip_pointer(text):
    """(text without our block, whether there was one)."""
    start = text.find(_POINTER_BEGIN)
    if start < 0:
        return text, False
    end = text.find(_POINTER_END, start)
    if end < 0:
        # A begin with no end: someone edited inside our block. Leaving it is
        # the only move that cannot delete their work.
        raise SettingsUnreadable(
            "the Blink block in config.toml has no end marker")
    end += len(_POINTER_END)
    if text[end:end + 1] == "\n":
        end += 1
    return text[:start] + text[end:], True


def install_pointer(config_path: str) -> str:
    """Point Codex's config at the hooks file. Idempotent.

    Refuses, and changes nothing, when a `hooks` table already exists outside
    our block: a duplicate table header is a TOML parse error, and a
    config.toml that does not parse is a Codex that does not start. Repairing
    someone's hand-written table is not ours to attempt.
    """
    text = _read_text(config_path)
    without, _had = _strip_pointer(text)
    for line in without.splitlines():
        stripped = line.strip()
        if stripped.startswith("[hooks]") or stripped.startswith("[hooks."):
            raise SettingsUnreadable(
                "config.toml already has a [hooks] table; left it alone")
    if without and not without.endswith("\n"):
        without += "\n"
    block = f"{_POINTER_BEGIN}\n{_POINTER_BODY}\n{_POINTER_END}\n"
    _write_text(config_path, without + block)
    return "Codex config points at the hooks file."


def remove_pointer(config_path: str) -> str:
    """Take our block back out, byte for byte. Never raises on absence."""
    try:
        text = _read_text(config_path)
    except SettingsUnreadable as e:
        return f"config.toml could not be read ({e}); left it alone."
    try:
        without, had = _strip_pointer(text)
    except SettingsUnreadable as e:
        return f"{e}; left it alone."
    if not had:
        return "No Codex config pointer to remove."
    _write_text(config_path, without)
    return "Codex config pointer removed."
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_install_codex_hooks.py -q`
Expected: PASS.

Verify the result parses as TOML, using the stdlib reader if the interpreter has one:

```bash
python3 -c "
import tomllib, pathlib, tempfile, sys
sys.path.insert(0, '.')
from pc import install_codex_hooks as ich
p = pathlib.Path(tempfile.mkdtemp())/'config.toml'
p.write_text('[projects.\"/x\"]\ntrust_level = \"trusted\"\n')
ich.install_pointer(str(p))
print(tomllib.loads(p.read_text()))
" || echo "no tomllib on this interpreter (3.10) -- check by eye instead"
```

Expected: a dict containing both `projects` and `hooks`, or the fallback message on Python 3.10.

Run: `pytest tests -q`
Expected: 565 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/install_codex_hooks.py tests/pc/test_install_codex_hooks.py
git commit -m "feat: point Codex's config at the hooks file, reversibly"
```

---

### Task 11: `blink install` registers the Codex hook, and says so first

**Files:**
- Modify: `pc/cli.py` (`cmd_install`: the step numbering, the `state` chmod loop, and a new step between the Claude hooks step and the service step)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `install_codex_hooks.install`, `install_codex_hooks.hooks_file`, `cli.codex_present`, `cli.hook_shim_path`.
- Produces: `cli._announce_codex_hooks() -> None` and `cli._install_codex_hooks() -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_install_says_codex_will_ask_before_it_writes(monkeypatch, capsys,
                                                      tmp_path):
    """Disclosure before the write, not after -- and specifically the trust
    prompt, because an unexplained dialog from a tool the user did not think
    they were configuring is a support incident, not a feature."""
    monkeypatch.setattr(cli, "codex_present", lambda: True)
    written = []
    monkeypatch.setattr(cli.install_codex_hooks, "install",
                        lambda p, s: written.append(p) or "installed (10 events).")

    cli._announce_codex_hooks()
    out_before = capsys.readouterr().out
    cli._install_codex_hooks()

    assert written, "the test must actually reach the installer"
    assert "trust" in out_before.lower()
    assert "Codex" in out_before
    for sentence in out_before.strip().splitlines():
        s = sentence.strip()
        if s:
            assert s[0].isupper() or s[0] in "-(", \
                f"copy is sentence case: {s!r}"


def test_install_skips_the_codex_step_when_codex_is_absent(monkeypatch):
    monkeypatch.setattr(cli, "codex_present", lambda: False)
    called = []
    monkeypatch.setattr(cli.install_codex_hooks, "install",
                        lambda p, s: called.append(p))
    msg = cli._install_codex_hooks()
    assert called == []
    assert "no Codex" in msg


def test_install_does_not_fail_when_the_codex_config_is_unreadable(monkeypatch):
    """The status line is the product; the activity light is a nicety. A hooks
    file we cannot safely edit costs a pip, not an install."""
    monkeypatch.setattr(cli, "codex_present", lambda: True)

    def boom(_p, _s):
        raise cli.install_statusline.SettingsUnreadable("not valid JSON")

    monkeypatch.setattr(cli.install_codex_hooks, "install", boom)
    msg = cli._install_codex_hooks()
    assert msg.startswith("skipped")
    assert "not valid JSON" in msg


def test_install_creates_the_codex_state_directory_private(monkeypatch,
                                                           tmp_path):
    """The slots name the projects someone has open. The default umask would
    leave them readable by every account on the machine."""
    import os
    import stat
    monkeypatch.setattr(cli, "codex_present", lambda: True)
    monkeypatch.setattr(cli.install_codex_hooks, "install",
                        lambda p, s: "installed (10 events).")
    cli._install_codex_hooks()
    d = os.path.join(cli.blink_home(), "state-codex")
    assert os.path.isdir(d)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_cli.py -q -k codex`
Expected: FAIL with `AttributeError: module 'pc.cli' has no attribute '_announce_codex_hooks'`.

- [ ] **Step 3: Implement**

In `pc/cli.py`, add `install_codex_hooks` to the imports beside `install_hooks`, then add these two functions near `_note_if_no_claude_code`:

```python
def _announce_codex_hooks() -> None:
    """Say what is about to happen in Codex, before it happens.

    Install is deliberately unattended, so disclosure is the only thing
    standing between us and configuring a second vendor's tool without saying
    so. The trust prompt is the part that has to be said out loud: Codex asks
    once, in its own interface, minutes or days later, and an unexplained
    dialog from a tool the person did not think they were configuring is a
    support incident rather than a feature.
    """
    print("Blink is about to add a hook to Codex as well.")
    print()
    print(f"  File     {install_codex_hooks.hooks_file()}")
    print("  Why      Codex does not record permission prompts in its session")
    print("           log, so without this a Codex session waiting on you")
    print("           looks idle on the panel.")
    print("  Note     The first time the hook runs, Codex will ask you once")
    print("           whether to trust it. That prompt is expected and Blink")
    print("           cannot answer it for you. Say yes and the amber light")
    print("           works; say no and everything else still does.")
    print()


def _install_codex_hooks() -> str:
    """Register the shim with Codex, if there is a Codex here to register with.

    Detected rather than asked about, the same way the Claude Code steps are:
    the product is meant to be plug-and-play. Absent Codex, nothing is written
    at all -- creating a hooks file for a tool that is not installed would
    leave a stranger's configuration on the machine.
    """
    if not codex_present():
        return "no Codex on this machine, nothing to do"
    # Private to the user, for the same reason ~/.blink/state is: these files
    # name the projects someone has open, and the default umask would leave
    # them readable by every account on the machine. Created here rather than
    # left to the shim so the mode is right from the first hook onward.
    state_dir = os.path.join(blink_home(), "state-codex")
    os.makedirs(state_dir, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass                 # Windows, where the mode does not apply
    try:
        return install_codex_hooks.install(install_codex_hooks.hooks_file(),
                                           hook_shim_path())
    except install_statusline.SettingsUnreadable as e:
        # Same judgement as the Claude hooks step above: the status line is
        # the product and the activity light is a nicety. A hooks file we
        # cannot safely edit costs the user a pip, and is not worth failing an
        # install that has otherwise worked -- still less worth "repairing"
        # another vendor's config by writing our idea of it over theirs.
        return f"skipped ({e})"
```

If Task 10 was run, add these two lines to `_install_codex_hooks`, immediately before the `return install_codex_hooks.install(...)`:

```python
        install_codex_hooks.install_pointer(install_codex_hooks.config_file())
```

(inside the same `try`, so a refusal there is reported as `skipped (...)` too).

Then in `cmd_install`, renumber the steps from `[N/4]` to `[N/5]` and insert the new one between the activity-hooks step and the background-service step:

```python
    print("[4/5] Codex hooks ... ", end="", flush=True)
    if codex_present():
        print()
        _announce_codex_hooks()
        print("      " + _install_codex_hooks())
    else:
        print(_install_codex_hooks())

    print("[5/5] Background service ... ", end="", flush=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_cli.py -q`
Expected: PASS.

Run: `sh tests/ci/check_install.sh` (skip with a note if it needs a built binary you do not have)

Run: `pytest tests -q`
Expected: 569 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/cli.py tests/test_cli.py
git commit -m "feat: blink install registers the Codex hook, after saying so"
```

---

### Task 12: `blink uninstall` removes it, and the slots with it

**Files:**
- Modify: `pc/cli.py` (`cmd_uninstall` step numbering and a new step; `_rm_state_dir`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `install_codex_hooks.uninstall`, and `install_codex_hooks.remove_pointer` if Task 10 ran.
- Produces: `_rm_state_dir()` clears `~/.blink/state-codex` as well as `~/.blink/state`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_uninstall_removes_the_codex_slots_too(tmp_path):
    """A left-behind slot directory is not litter, it is a lie: the daemon
    would go on counting sessions from a tool it no longer hooks."""
    import os
    for sub in ("state", "state-codex"):
        d = os.path.join(cli.blink_home(), sub)
        os.makedirs(os.path.join(d, "sess-1"), exist_ok=True)
        with open(os.path.join(d, "sess-1.state"), "w") as f:
            f.write("{}")
        with open(os.path.join(d, "sess-1", "agent-1"), "w") as f:
            f.write("")

    cli._rm_state_dir()

    for sub in ("state", "state-codex"):
        d = os.path.join(cli.blink_home(), sub)
        assert not os.path.exists(os.path.join(d, "sess-1.state"))
        assert not os.path.exists(os.path.join(d, "sess-1"))


def test_uninstall_keeps_going_when_the_codex_file_is_unreadable(monkeypatch,
                                                                 capsys):
    """The login service is already gone by this point in cmd_uninstall, so
    stopping here would leave the machine half-undone."""
    def boom(_p, _s=None):
        raise cli.install_statusline.SettingsUnreadable("not valid JSON")

    monkeypatch.setattr(cli.install_codex_hooks, "uninstall", boom)
    cli._uninstall_codex_hooks()
    out = capsys.readouterr().out
    assert "Left alone" in out
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_cli.py -q -k "codex_slots or codex_file_is_unreadable"`
Expected: FAIL — `state-codex` survives `_rm_state_dir`, and `_uninstall_codex_hooks` does not exist.

- [ ] **Step 3: Implement**

In `pc/cli.py`, add beside `_install_codex_hooks`:

```python
def _uninstall_codex_hooks() -> None:
    """Take the Codex registration back out, and never stop on a bad file.

    Prints rather than returning, because unlike install this has two things
    to say when Task 10's pointer is in play, and because the caller has
    already removed the login service by the time it runs: stopping here would
    leave the machine half-undone. And the one thing we must not do is
    "repair" a file we cannot parse by writing a fresh one over it.
    """
    try:
        print("      " + install_codex_hooks.uninstall(
            install_codex_hooks.hooks_file(), hook_shim_path()))
    except install_statusline.SettingsUnreadable as e:
        print(f"      Left alone: {e}")
```

If Task 10 ran, add inside the same function after the first print:

```python
    print("      " + install_codex_hooks.remove_pointer(
        install_codex_hooks.config_file()))
```

Change `_rm_state_dir` to cover both directories:

```python
def _rm_state_dir():
    """Remove ~/.blink/state and ~/.blink/state-codex and their subdirectories.

    Two levels deep and no deeper, by construction: the shim only ever creates
    <session>.state files and <session>/<agent> files, in whichever of the two
    directories its second argument chose. Walking rather than shutil.rmtree
    because this runs against a path under the customer's home and a bounded
    loop cannot be talked into deleting more than it was told.
    """
    for sub in ("state", "state-codex"):
        root = os.path.join(blink_home(), sub)
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            p = os.path.join(root, name)
            if os.path.isdir(p):
                for inner in (os.listdir(p) if os.path.isdir(p) else []):
                    _rm(os.path.join(p, inner))
```

Keep whatever the existing function does after this loop (the `os.rmdir`/`_rm` of the leaf and the root) inside the `for sub` loop, indented one level further — read the current body at `pc/cli.py:1117` and move the whole of it under the new `for sub in (...)` header rather than retyping it.

Then in `cmd_uninstall`, renumber `[N/4]` to `[N/5]` and insert:

```python
    print("[4/5] Codex hooks:")
    _uninstall_codex_hooks()

    print("[5/5] Files ... ", end="", flush=True)
```

Add `os.path.join(blink_home(), "codex-hooks-installed-commands")` to the list of files `cmd_uninstall` removes, beside `hooks-installed-commands` if that is already there; if it is not, add both — a marker left behind after an uninstall is what makes a later install think it never happened.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_cli.py -q`
Expected: PASS.

Run: `pytest tests -q`
Expected: 571 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/cli.py tests/test_cli.py
git commit -m "feat: blink uninstall removes the Codex hook and its slots"
```

---

### Task 13: `blink status` says whether the Codex hook is firing

The most useful support answer after "is it installed" is "is it running". For Codex there is a third question nobody else has — "did you say yes to the trust prompt" — and a registered hook that has never written a slot is exactly what saying no looks like.

**Files:**
- Modify: `pc/cli.py` (the source-status block that prints the `Codex` line, around `pc/cli.py:1357`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `install_codex_hooks._read_marker`, `pc.providers.codex_state.scan`.
- Produces: `cli._codex_hook_status() -> list[str]` — the lines to print under the `Codex` heading.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_status_reports_codex_hooks_not_installed(monkeypatch):
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker", lambda: set())
    lines = cli._codex_hook_status()
    assert any("not installed" in ln for ln in lines)


def test_status_reports_a_registered_hook_that_has_never_fired(monkeypatch):
    """Which is exactly what declining the trust prompt looks like, and the
    single most likely support call this feature will generate."""
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker",
                        lambda: {"sh /x/blink-hook.sh Stop codex"})
    monkeypatch.setattr(cli.codex_state, "scan",
                        lambda now, path=None, sweep=True: ({}, 0))
    lines = cli._codex_hook_status()
    assert any("trust" in ln.lower() for ln in lines)


def test_status_reports_live_codex_sessions(monkeypatch):
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker",
                        lambda: {"sh /x/blink-hook.sh Stop codex"})
    monkeypatch.setattr(cli.codex_state, "scan",
                        lambda now, path=None, sweep=True: (
                            {"a": "running", "b": "waiting"}, 0))
    lines = cli._codex_hook_status()
    assert any("2" in ln for ln in lines)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_cli.py -q -k codex_hook`
Expected: FAIL with `AttributeError: module 'pc.cli' has no attribute '_codex_hook_status'`.

- [ ] **Step 3: Implement**

In `pc/cli.py`, add `from pc.providers import codex_state` to the imports used by the status block (or import it inside the function, matching whichever style the surrounding code already uses), and add:

```python
def _codex_hook_status():
    """Lines describing the Codex hook, for `blink status`.

    Three states worth telling apart, because the fixes are different:
    registered and firing, registered and silent, not registered. The middle
    one is what declining Codex's trust prompt looks like from out here -- the
    entries are in the file, Codex simply never runs them -- and it is the
    single most likely support call this feature will produce.
    """
    if not install_codex_hooks._read_marker():
        return ["Codex hook  not installed"
                " (run `blink install` with Codex on this machine)"]
    try:
        states, _agents = codex_state.scan(time.time(), sweep=False)
    except Exception:
        states = {}
    if not states:
        return [
            "Codex hook  registered, but it has never written anything",
            "            Codex asks once whether to trust a hook. If that",
            "            prompt was declined, the hook is in the file and",
            "            never runs -- open Codex and accept it.",
        ]
    return [f"Codex hook  firing, {len(states)} live session(s)"]
```

and print those lines from the source-status block, immediately after the existing `Codex ...` rollout lines:

```python
    for line in _codex_hook_status():
        print(line)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_cli.py -q`
Expected: PASS.

Run: `python3 -m pc.cli status` (from the checkout) and read the Codex block by eye.

Run: `pytest tests -q`
Expected: 574 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/cli.py tests/test_cli.py
git commit -m "feat: blink status distinguishes a silent Codex hook from a missing one"
```

---

### Task 14: Run it on the desk against the installed Codex

Nothing above this line has ever been executed by Codex. Every task before it is written against a contract read out of a binary and a source tree. **This feature is not done until this task passes**, and the memory note that says "flashed and boot-verified, not just built" applies here in its software form: registered and observed firing, not just written.

**Files:**
- Modify: `docs/research/codex-hook-contract.md` (fill in "What could not be established without running one")
- Test: manual, on this machine, against `codex-cli 0.150.0` at `/Users/KfirLevy/.local/bin/codex`.

**Interfaces:**
- Consumes: everything.
- Produces: a filled-in findings document and a green or red verdict.

- [ ] **Step 1: Install from the checkout and read the disclosure**

```bash
cd /Users/KfirLevy/Projects/LiveClaudeUi/.claude/worktrees/hint-line
cp ~/.codex/config.toml /tmp/codex-config.backup.toml
cp -r ~/.codex/hooks /tmp/codex-hooks.backup 2>/dev/null || true
python3 -m pc.cli install
```

Expected: step `[4/5] Codex hooks` prints the disclosure paragraph BEFORE the installer line, and the installer line says `Codex state hooks installed (10 events).`

Confirm on disk:

```bash
cat "${CODEX_HOME:-$HOME/.codex}/hooks.json"
cat "${CODEX_HOME:-$HOME/.codex}/config.toml"
cat ~/.blink/codex-hooks-installed-commands
```

- [ ] **Step 2: Make Codex fire a hook, and watch for the trust prompt**

```bash
cd /tmp && mkdir -p blink-codex-probe && cd blink-codex-probe
codex
```

Type a prompt that runs one shell command. **Record exactly what Codex asks about trust** — the wording, when it appears, and what accepting it writes. Then in a second terminal:

```bash
ls -la ~/.blink/state-codex/
cat ~/.blink/state-codex/*.state
```

Expected: at least one `<uuid>.state` file containing `{"event":"...","t":<10 digits>,"name":"blink-codex-probe"}`.

If the directory is empty: the trust prompt was declined, or F1/F2/F3 are wrong. Re-run Task 1's probes, correct `_HOOKS_PATH_PARTS` / `_EVENTS_KEY` / `_POINTER_BODY`, and repeat. **Do not proceed past this step on an empty directory.**

- [ ] **Step 3: Confirm the waiting state, and confirm it clears**

Start Codex with approvals on so a tool call blocks:

```bash
codex --sandbox read-only
```

Ask it to run a command that needs approval, and while the prompt is on screen:

```bash
cat ~/.blink/state-codex/*.state
python3 -c "
import time, sys; sys.path.insert(0, '/Users/KfirLevy/Projects/LiveClaudeUi/.claude/worktrees/hint-line')
from pc.providers import codex_cli
for f in codex_cli.CodexCliProvider().poll(time.time()):
    print(f.src, f.state, f.n_run, f.n_wait, f.n_idle)
"
```

Expected while the prompt is up: `"event":"PermissionRequest"`, and a frame reading `cli-state waiting 0 1 0`.

Then answer the prompt and run the same two commands again. Expected: the event is no longer `PermissionRequest` and `n_wait` is 0. **Repeat for all three exits** — approve, deny, and Esc — and record which event each one produced. A waiting state that any of the three fails to clear is a stop-the-line defect, not a note.

- [ ] **Step 4: Confirm the census is not doubled and the two tools stay apart**

With one Codex session and one Claude Code session open:

```bash
python3 -c "
import time, sys; sys.path.insert(0, '/Users/KfirLevy/Projects/LiveClaudeUi/.claude/worktrees/hint-line')
from pc import ingest, protocol
bus = ingest.IngestionBus()
print(bus.poll())
"
ls ~/.blink/state/ ~/.blink/state-codex/
```

Expected: the session counts on the wire message equal the number of terminals actually open, and no file appears in both directories. Also confirm the ids match between the two sources — this is what Task 7's union depends on:

```bash
python3 -c "
import glob, os, sys; sys.path.insert(0, '/Users/KfirLevy/Projects/LiveClaudeUi/.claude/worktrees/hint-line')
from pc.providers import codex_cli
print('rollout ids:', [codex_cli.rollout_session_id(p) for p in codex_cli.recent_rollouts()])
print('hook ids:   ', [os.path.basename(p)[:-6] for p in glob.glob(os.path.expanduser('~/.blink/state-codex/*.state'))])
"
```

Expected: the id of the live session appears in both lists, spelled identically. **If the spellings differ**, the union is de-duplicating nothing and every live Codex session is counted twice — stop, record the two spellings in the findings document, and re-open Task 7's key choice before shipping.

- [ ] **Step 5: Confirm `codex exec` counts, and that uninstall is clean**

```bash
cd /tmp/blink-codex-probe && codex exec "print hello and stop"
ls -la ~/.blink/state-codex/
```

Expected: a batch run leaves a slot too. A `codex exec` run is a session for these purposes and must show a pip.

```bash
python3 -m pc.cli uninstall
cat "${CODEX_HOME:-$HOME/.codex}/hooks.json"
cat "${CODEX_HOME:-$HOME/.codex}/config.toml"
ls ~/.blink/state-codex 2>&1
diff /tmp/codex-config.backup.toml "${CODEX_HOME:-$HOME/.codex}/config.toml"
```

Expected: our entries gone, the user's config byte-identical to the backup (`diff` silent), `~/.blink/state-codex` empty or absent, and no `codex-hooks-installed-commands` marker left.

- [ ] **Step 6: Write down what running it settled**

Fill in "What could not be established without running one" in `docs/research/codex-hook-contract.md` with: the exact trust-prompt wording and where the `trusted_hash` was written; which event each of the three approval exits produced; whether `SubagentStart` carried an `agent_id`; whether `codex exec` fired `SessionStart`/`SessionEnd`; and the id spellings from Step 4.

- [ ] **Step 7: Commit**

```bash
git add docs/research/codex-hook-contract.md
git commit -m "docs: what running a Codex hook on the desk actually settled"
```

---

## Judgement calls, stated

**Codex installed but its config unreadable or hand-edited — refuse, do not repair.** `pc/install_statusline._load` has raised `SettingsUnreadable` on this exact ground since it was written, on the reasoning that a file which fails to parse is usually a file someone is halfway through editing. The argument is stronger here, not weaker: this is another vendor's configuration, documented as a place people put their own automation, and "repairing" it means writing our idea of it over theirs. So `install()` refuses and changes nothing, `cmd_install` prints `skipped (...)` and carries on (the status line is the product; the activity light is a nicety), and the TOML pointer in Task 10 refuses outright rather than risk producing a duplicate `[hooks]` header, which would be a parse error and therefore a Codex that does not start.

**One shim, not two.** The valuable thing in `tools/blink-hook.sh` is not its control flow — it is `_ident` and `_projname`, each of which is the fix for a bug that reached a real machine (a tool argument becoming a filename, `..` reaching `~/.blink` where the signing keys live, a nested `cwd` promoting attacker-chosen text onto a display other people see). A second copy is a second place for the next such fix to be forgotten, and this codebase has already been bitten by exactly that shape of duplication. Codex's event names are the same words Claude Code's are and its command hooks are fed the same stdin fields, so the two tools genuinely differ in one respect only: which directory the slot goes in. The cost on the hot path is one parameter expansion and a two-arm `case` — no extra process — on a path that already forks `sed` twice. The `case` is a whitelist rather than a path fragment because the argument arrives from a config file people can hand-edit.

**No watchdog over the Codex REGISTRATION -- but the shim file itself is covered.** Two different things live under this heading and the distinction decides the answer.

The *registration* -- the entry in Codex's own config -- gets no watchdog. `DriftWatchdog` exists for `statusLine`, a single slot that Claude Code's updates and other tools can displace without telling anyone. Codex's hook entry is not that shape, and there is a specific reason not to watch it: a watchdog would have the daemon writing unattended into another vendor's configuration every 300 seconds, and every such write invalidates Codex's `trusted_hash` and re-prompts the user. A self-healing feature whose healing action is "silently ask the customer to approve something again, from a background process" is worse than the fault it repairs. `blink status` (Task 13) says the hook has gone quiet; `blink install` puts it back.

The *shim file* is the opposite case, and the plan `docs/plans/shim-self-repair.md` must cover it. That plan adds a content check that rewrites any shim under `~/.blink/` whose bytes have fallen behind the daemon -- the fault that made session naming a silent no-op on every install that arrived by `blink update`. A Codex shim living in `~/.blink/` inherits that protection for free, and must: it is the same failure mode, and rewriting our own file in our own directory touches nothing Codex has hashed.

So when this plan lands, add the Codex shim to the `shims=` tuple that plan passes to `DriftWatchdog`. If you are reading this before that plan has been executed, that is the ordering: repair first, then this.

## Self-review

**Spec coverage.** Q2 of the research file — "waiting for you" via Codex's hooks — is Tasks 2, 5, 7, 8. Its warning (1), the trust prompt, is Tasks 1 (F5), 11 (the copy), 13 (the "registered but silent" line) and 14 (the observed wording). Warning (2), the unpinned TOML/JSON shape, is Task 1 and the two switch constants in Task 8. Warning (4), the `.state` namespace collision and wrong provider attribution, is the separate directory in Task 2 and its `14b` assertion, plus Task 5's `test_the_default_directory_is_not_the_claude_one`. The brief's requirement that `pc/protocol.py` still sums correctly is Task 7 — the counts land on the existing `codex` provider frame rather than a third provider that `select_pair` would drop, and `n_wait` is added because `protocol._pair_from` reads it. `codex exec` counting as a session is Task 14 Step 5. Uninstall symmetry is Tasks 9 and 12. **Deliberately not covered:** research Q1 (the project name on the Codex frame) and Q3 (`task_complete` carrying an `error`) — both are rollout-reader features with their own risks, and this plan touches `codex_cli.poll` narrowly enough that they remain a separate piece of work. `tests/ci/check_codex_contract.sh` is likewise left alone; the research file's note that it pins nothing about `task_started`/`task_complete` naming is a real gap, and it belongs to the Q3 plan.

**Placeholder scan.** Two places name a decision rather than a value: Task 8's `_HOOKS_PATH_PARTS` / `_EVENTS_KEY` and Task 10's `_POINTER_BODY`. Each has a concrete default written out, each names the exact Task 1 finding that confirms or changes it, and both branches of `_EVENTS_KEY` are implemented and tested — this is a pinned switch, not a gap. Task 10 is conditional on F3, with the skip condition stated. Task 12 tells the implementer to move the existing body of `_rm_state_dir` under a new loop header rather than reprinting it; that is a lift of code already in the repository at a named line, not an unwritten step.

**Type consistency.** `session_states` returns `({sid: (state, name)}, agents)` in Task 4 and is destructured that way in Task 5. `codex_state.scan(now_epoch, path=None, sweep=True)` returns `({sid: state}, agents)` in Task 5 and is called with exactly those keywords in Task 7 and Task 13. `rollout_session_id(path) -> str` in Task 6 is called as a plain string in Task 7's `rollout_states[rollout_session_id(path) or path]`. `hook_command(shim_path, event)` has the same signature in Task 8 as `install_hooks.hook_command`, and both `install` and `uninstall` in Tasks 8 and 9 take `(hooks_path, shim_path)` — matching `install_hooks`, so `pc/cli.py` calls them the same way it calls the Claude pair. `STATE_SRC_ID` is used unchanged from `codex_cli`; the state frame's `src` does not change, so nothing downstream of `pc/normalizer` has to learn a new source name.

**Fixed inline during review:** Task 7's frame originally carried no `n_wait` (copied from the existing code, which had no waiting state to report) — an amber pip beside "0 sessions" on the panel. Task 3 was added after tracing `Interrupt` through `derive_state` and finding it returns `STATE_UNKNOWN`, which drops the session from the census entirely rather than merely mislabelling it.

---

# DISCOVERY GATE — RUN 2026-09-04. Read this before any task below.

**Verdict: the premise HOLDS.** Codex 0.150.0 has a real, stable, on-by-default
hooks system. Verified, not inferred: `codex features list` reports
`hooks stable true`, `--help` documents `--dangerously-bypass-hook-trust`, and
upstream tag `rust-v0.150.0` carries the whole `codex-rs/hooks/` crate plus an
official docs page. Twelve events exist, including **`PermissionRequest`** —
"runs when Codex is about to ask for approval", which is exactly the WAITING
signal this plan was written for.

**Still true: no hook has ever been executed on this machine.** The first task
must be the smoke test, before anything is built on top.

## Four findings that change tasks below — do not implement around them

**1. The trust hash covers the declared COMMAND STRING, not the script.**
This inverts the risk the plan assumed. Editing the shim's *contents* does NOT
re-prompt; changing the *path* does. So `blink update`, which swaps the program
directory, would **silently disable the hook** if the command embeds a versioned
path. Register a STABLE entry-point path that survives updates, and give the
user a one-time `/hooks` trust instruction. The plan's warning about rewriting
`~/.codex` on a timer was aimed at the wrong hazard.

**2. De-duplicate on `transcript_path`, NOT on `session_id`.** Hook input
carries `transcript_path` — the rollout file itself — so the filename is the
join key, and it is the same value the rollout reader already has. This
sidesteps a real trap: the official docs show a session id shaped `thr_123`,
NOT the UUID the rollout filename uses. The plan's assumption that the two ids
are spelled the same was never safe, and this makes it moot.

**3. Approve and deny have no events of their own.** Esc fires `Interrupt`
(with a hard 1-3 s timeout). Approve is followed by `PostToolUse` once the tool
finishes; deny fires nothing. So a WAITING state is always clearable — at the
next `PreToolUse` or at `Stop` — but on a deny it clears LATE. Decide
deliberately how long a stale WAITING may linger, and say so in the copy.

**4. `permission_mode` is on the hook input.** BLINK can suppress WAITING
entirely in never-ask modes, where an approval prompt cannot occur.

## The question that would have made this plan unnecessary, and its answer

Could the waiting signal come from the rollout files BLINK already reads, with
no hook at all? **No — verified impossible at the shipped tag.**
`ExecApprovalRequest`, `ApplyPatchApprovalRequest`, `RequestPermissions` and
`RequestUserInput` all sit in the "transient, never persisted" arm of
`codex-rs/rollout/src/policy.rs`, and a census of all four real rollout files on
this machine found no approval-shaped event. The hook path is necessary, not
merely convenient.

Full labelled findings (VERIFIED / LIKELY / UNKNOWN per claim):
`scratchpad/codex-hooks-gate.md` from the 2026-09-04 session.
