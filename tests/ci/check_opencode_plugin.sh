#!/bin/sh
# Exercise tools/blink-opencode.js the way OpenCode drives it, under node.
#
#   tests/ci/check_opencode_plugin.sh
#
# The plugin is the OpenCode half of what tools/blink-hook.sh is for Claude
# Code and Codex: it turns lifecycle events into the files under
# ~/.blink/state-opencode that pc/providers/claude_state.py reads. Like the
# shim, it takes ids out of a payload and uses them as FILENAMES, so the
# traversal and injection cases in the driver are the point of this file, not
# extras -- and like the shim, it runs inside somebody else's terminal, so a
# byte on stdout or stderr is a bug in their session.
#
# The scenarios live in tests/ci/opencode_plugin_driver.mjs, because asserting
# on a plugin's in-memory state from sh would mean a node process per step and
# the throttle the plugin has (a running slot is rewritten at most every five
# seconds) is measured within one process. What stays here is what only an
# outside observer can check: that the process printed nothing at all, and
# what its exit left behind.
#
# Node and not Bun, which is what OpenCode actually runs plugins under: the
# plugin imports node: builtins only, which Bun implements, and CI runners have
# node. The end-to-end run against a real OpenCode is recorded in the PR.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

command -v node >/dev/null 2>&1 || { echo "node not installed" >&2; exit 1; }

DRIVER="$ROOT/tests/ci/opencode_plugin_driver.mjs"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/blink-opencode.XXXXXX")
trap 'chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT
DIR="$WORK/home/.blink/state-opencode"

printf '== opencode plugin under node %s\n' "$(node --version)"

# 1. Every scenario, silently. The driver traps the plugin's own writes to
#    stdout and stderr in-process and fails on any; this checks the process
#    as a whole, which also covers a warning Node itself prints on the
#    plugin's behalf (a listener leak, a deprecation).
mkdir -p "$WORK/home"
status=0
HOME="$WORK/home" USERPROFILE="$WORK/home" \
	node "$DRIVER" check >"$WORK/out.txt" 2>"$WORK/err.txt" || status=$?
[ "$status" -eq 0 ] || fail "scenarios failed:
$(cat "$WORK/err.txt")"
[ -s "$WORK/out.txt" ] && fail "printed to stdout: $(cat "$WORK/out.txt")"
[ -s "$WORK/err.txt" ] && fail "printed to stderr: $(cat "$WORK/err.txt")"
ok "every scenario, with nothing printed"

# 2. Nothing outside the state directory. The hostile ids in the driver are
#    aimed at ~/.blink and above; whatever they managed would be here.
stray=$(cd "$WORK/home" && find . -mindepth 1 \
	! -path './.blink' ! -path './.blink/state-opencode' \
	! -path './.blink/state-opencode/*' -print)
[ -z "$stray" ] || fail "wrote outside ~/.blink/state-opencode: $stray"
ok "writes nothing outside ~/.blink/state-opencode"

# 3. The process exiting ends its sessions, as closing a terminal fires
#    SessionEnd in Claude Code: the slot goes, a tombstone stays, and the
#    subagent's directory goes with its session.
#    The state directory is made first, 0700, as `blink install` makes it:
#    the plugin writes only into a directory that already exists.
rm -rf "${WORK:?}/home"
mkdir -p "$WORK/home/.blink"
mkdir -m 700 "$DIR"
HOME="$WORK/home" USERPROFILE="$WORK/home" \
	node "$DRIVER" exit >"$WORK/out.txt" 2>"$WORK/err.txt" ||
	fail "exit scenario failed: $(cat "$WORK/err.txt")"
[ -s "$WORK/out.txt" ] && fail "printed to stdout on exit: $(cat "$WORK/out.txt")"
[ -s "$WORK/err.txt" ] && fail "printed to stderr on exit: $(cat "$WORK/err.txt")"
[ -e "$DIR/ses_exit.state" ] && fail "the slot outlived the process"
[ -e "$DIR/ses_exit" ] && fail "the agent directory outlived the process"
[ -e "$DIR/ses_exit.ended" ] || fail "no tombstone after exit: $(ls -a "$DIR")"
[ -e "$DIR/ses_exitkid.ended" ] && fail "a subagent got a tombstone of its own"
ok "exit ends the process's sessions and leaves tombstones"

# 3b. The same run with no state directory: a HOME that `blink uninstall`
#     has been through while OpenCode kept running. Found in review,
#     2026-10-19: the plugin used to create ~/.blink/state-opencode itself,
#     so a session still open across an uninstall put it back. Nothing at
#     all may appear under HOME, the exit handler's tombstones included, and
#     the process must exit 0 having printed nothing.
rm -rf "${WORK:?}/home"
mkdir -p "$WORK/home"
HOME="$WORK/home" USERPROFILE="$WORK/home" \
	node "$DRIVER" exit >"$WORK/out.txt" 2>"$WORK/err.txt" ||
	fail "exit scenario without a state directory failed: $(cat "$WORK/err.txt")"
[ -s "$WORK/out.txt" ] && fail "printed to stdout without a state dir: $(cat "$WORK/out.txt")"
[ -s "$WORK/err.txt" ] && fail "printed to stderr without a state dir: $(cat "$WORK/err.txt")"
made=$(cd "$WORK/home" && find . -mindepth 1 -print)
[ -z "$made" ] || fail "wrote into a HOME with no state directory: $made"
ok "writes nothing, and creates nothing, when the state directory is absent"

# 4. The marker is the first line, byte for byte: the installer refuses to
#    touch a blink.js without it, so a reworded first line would make every
#    installed copy look like somebody else's file.
head -n 1 "$ROOT/tools/blink-opencode.js" | grep -q '^// blink-opencode-plugin: ' ||
	fail "the first line is not the ownership marker"
ok "the ownership marker is the first line"

# 5. node: builtins only. A bare specifier would make OpenCode run npm in the
#    user's config directory before the plugin could load.
bad=$(grep -nE '^[[:space:]]*import[^"]*"[^"]*"' "$ROOT/tools/blink-opencode.js" |
	grep -vE '"node:[a-z_/]+"' || true)
[ -z "$bad" ] || fail "imports something other than node: builtins: $bad"
grep -nE 'require\(|import\(' "$ROOT/tools/blink-opencode.js" >/dev/null &&
	fail "loads code dynamically"
ok "imports node: builtins only"

# 6. Nothing prints. A console call that never fires in these scenarios would
#    still fire in some other one.
grep -nE 'console\.|process\.(stdout|stderr)' "$ROOT/tools/blink-opencode.js" &&
	fail "the plugin has a code path that prints"
ok "has no code path that prints"

printf 'opencode plugin: all checks passed\n'
