# Left open by the burn-view reviews (2026-11-01)

**Status:** OPEN. Four `/code-review max` rounds ran over PR #5 (the Claude
Ultra and Codex Pro burn view). Everything that could put a Pro or Max desk on
the burn view, and every regression a fix round introduced, was fixed before
merge. These were confirmed or found plausible and deliberately left, because
each one predates the branch or could not be measured here. The reviewers'
repro scripts were scratch files and are gone; each entry says enough to
rebuild one.

## `blink install` writes to the wrong settings.json when CLAUDE_CONFIG_DIR is set

`settings_path()` is `~/.claude/settings.json` whatever `CLAUDE_CONFIG_DIR`
says, but Claude Code 2.1.316 reads its user settings from
`$CLAUDE_CONFIG_DIR/settings.json` (read in its bundle, not run). On such a
desk Blink's status line and hooks are installed where Claude Code never
looks: no `statusline.json`, no Claude Code figures, no activity light and no
status-line evidence for the Ultra verdict, while `blink status` says both are
installed. Older than the branch. The fix has to agree with the service
environment question below: the shell may set the variable that the login
service never sees.

## `blink status` reads CODEX_HOME from the shell, the service does not

`_source_lines_codex` and `_wire_line` resolve `CODEX_HOME` in the shell
`blink status` runs in. The launchd and systemd services never carry it, so on
a Mac with `export CODEX_HOME=...` in `.zshrc` the board's Codex page is blank
while `blink status` reports the Codex log as parsed and fresh. The install note
was fixed for this on the branch; the status command was not. Older than the
branch.

## systemd --user may see the login session's environment after all

`service_environ()` answers "nothing" for systemd. A `systemd --user` unit
inherits the user manager's environment, which GNOME, KDE and the Debian/Ubuntu
Xsession fill from the login session (`pc/theme_server._linux_session_env`
already relies on that), so the install note's "the background service does
not see it" may be wrong there, and the folder can change between a boot-time
start and a later restart. Plausible, not reproduced: no Linux desktop was to
hand. `systemctl --user show-environment` on one would settle it.

## A Codex reading stamped in the future wins the frame contest

`CLOCK_SLACK_S` keeps an ahead-of-clock reading out of the remembered
no-window word, but `poll()` still picks `best` by stamp, so a weekly-only line
stamped an hour ahead (a fast clock later corrected, a synced rollout) beats an
honest current reading with a 300-minute window. The Codex page then shows the
burn view on an account that has a five-hour limit, until Codex writes a later
stamp. The frame contest is older than the branch; the fix is to cap stamps at
now plus a slack in `_observed_at` for the contest too.

## Smaller

- `SessionVerdict.observe` lets ahead-dated evidence win when a source re-sends
  it on every poll.
- The sibling `_finite_num` helpers still raise OverflowError on a 310-digit
  integer.
- The watchdog's settings.json repair still retries ten times with sleeps on
  the serial thread.
- Under `BLINK_SCENARIO` one ScriptedProvider holds one frame, so a bench
  scenario cannot put two burn pages on a board at once.
- Windows: the walk takes size and mtime from `DirEntry.stat()`, which can be
  stale for a file another process holds open (Codex holds its rollout open
  for the session). Unmeasured.
