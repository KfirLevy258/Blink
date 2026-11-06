#!/bin/sh
# Check the installer, and the payload contract it depends on, against whatever
# Claude Code version is currently on PATH. CI installs a pinned version first.
#
#   tests/ci/check_claude_version.sh
#
# WHAT THIS CANNOT DO: make Claude Code render a status line. That needs a
# signed-in account, and CI has no credentials -- so nothing here proves the
# shim is actually invoked, or that rate_limits is populated for a real user.
# What it does prove is narrower and still worth gating on:
#
#   1. the version ships the payload contract we read (rate_limits ->
#      five_hour/seven_day -> used_percentage). This is the early warning for
#      the risk that matters most: we depend on a field with no stability
#      commitment, and shipped units go dark together the day it moves.
#   2. the shipped binary runs alongside that version and writes a
#      settings.json the CLI still starts with.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"
ci_binary

WORK="${TMPDIR:-/tmp}/blink-claude-version"

py() {
	# The Windows runners ship `python`, not always `python3`.
	if command -v python3 >/dev/null 2>&1; then python3 "$@"; else python "$@"; fi
}


command -v claude >/dev/null 2>&1 || fail "no claude on PATH"
VERSION=$(claude --version 2>/dev/null || echo "unknown")
printf '== Claude Code: %s\n' "$VERSION"

# ------------------------------------------------------- the payload contract

# Where the strings live depends on how the release was packaged, and that
# changed inside 2.1.x: older versions shipped one large cli.js bundle, newer
# ones ship a small launcher whose postinstall fetches a native binary. So look
# at the resolved entry point AND the installed package tree, and accept a hit
# from either -- pinning one layout would make this pass or fail for reasons
# that have nothing to do with the contract.
CLI=$(command -v claude)
RESOLVED=$(readlink -f "$CLI" 2>/dev/null || echo "$CLI")
SEARCH="$RESOLVED"
if command -v npm >/dev/null 2>&1; then
	pkg="$(npm root -g 2>/dev/null)/@anthropic-ai/claude-code"
	[ -d "$pkg" ] && SEARCH="$SEARCH $pkg"
fi

found_in=""
for field in rate_limits used_percentage five_hour seven_day; do
	hit=""
	for target in $SEARCH; do
		if [ -d "$target" ]; then
			grep -rqa "$field" "$target" 2>/dev/null && hit="$target"
		else
			grep -qa "$field" "$target" 2>/dev/null && hit="$target"
		fi
		[ -n "$hit" ] && break
	done
	[ -n "$hit" ] || fail "$VERSION does not carry '$field' -- the status line payload
       contract this product reads has changed. Nothing on a customer's desk
       will show a number until the daemon's mapping is updated. Check
       pc/statusline_source.py against the new payload before shipping."
	found_in="$hit"
done
ok "payload contract present (rate_limits, five_hour, seven_day, used_percentage) in $found_in"

sl_hit=""
for target in $SEARCH; do
	grep -rqa "statusLine" "$target" 2>/dev/null && sl_hit="$target" && break
done
[ -n "$sl_hit" ] ||
	fail "$VERSION has no statusLine support at all -- the installer's one setting
       does not exist in this version. It is below the floor this product
       supports; say so in the README rather than shipping into it."
ok "statusLine setting supported"

# ------------------------------------------------------------- the installer

rm -rf "$WORK"
HOME="$WORK/home"
mkdir -p "$HOME/.claude"
export HOME
# Windows resolves ~ from USERPROFILE -- for the binary AND for Claude Code,
# which is the point of running this job there: the two must agree on where
# settings.json is, or the installer edits a file the CLI never reads.
case "$(uname -s)" in
MINGW* | MSYS* | CYGWIN*)
	USERPROFILE=$(cygpath -w "$HOME")
	export USERPROFILE ;;
esac
printf '%s\n' '{"model": "opus"}' >"$HOME/.claude/settings.json"

# This script asserts about settings.json and nothing about the login service,
# and its HOME is a throwaway: the service manager is global, so without this
# the install below registered (and the uninstall removed) a real service on
# the runner, and since the binary refuses to touch the service from a HOME
# that is not the account's, it now reports "the background service was NOT
# installed" and exits 1 (CI run 41192997263, every claude-version job).
# Like check_update.sh and the smoke test, it asks for no service. And it is
# not run on a desk: see the header of check_install.sh.
BLINK_SKIP_SERVICE=1
export BLINK_SKIP_SERVICE

"$BIN" >"$WORK/out.txt" 2>&1 || {
	cat "$WORK/out.txt" >&2
	fail "the installer failed alongside $VERSION"
}
ok "installer runs alongside $VERSION"

py - "$HOME/.claude/settings.json" <<'EOF' || exit 1
import json, sys
d = json.load(open(sys.argv[1]))
assert d["model"] == "opus", "unrelated key lost"
cmd = d["statusLine"]["command"]
# The product's own platform rule: `bash <path>` on Windows (Claude Code
# rewrites a .sh command that does not start with it), `sh <path>` elsewhere;
# the path is quoted when it needs to be (the runner's temp dir has a ~).
import sys as _s
assert cmd.startswith("bash " if _s.platform == "win32" else "sh "), cmd
assert "blink-statusline.sh" in cmd, cmd
EOF
ok "settings.json is valid JSON with our key and their keys intact"

# The CLI must still start with the file we just edited. A version that choked
# on our write would fail here rather than in someone's terminal.
claude --version >/dev/null 2>&1 || fail "claude --version fails after install"
ok "claude still starts with our settings.json in place"

"$BIN" uninstall >"$WORK/undo.txt" 2>&1 || {
	cat "$WORK/undo.txt" >&2
	fail "uninstall failed"
}
claude --version >/dev/null 2>&1 || fail "claude --version fails after uninstall"
ok "uninstall clean, claude still starts"

# ------------------------------------------------------- the /usage contract
#
# The opt-in refresh runs `claude -p /usage` with the flags in
# pc/usage_refresh.command(), and judges the JSON with judge(). Neither has a
# stability commitment from Claude Code, the same risk as the status line
# field above. Logged out (this HOME has no login, and the runner has no
# keychain), every flag must still be accepted and the answer must be the
# zero-cost, zero-turn JSON result judge() reads as OK.
#
# Except below usage_refresh.USAGE_SINCE (2.1.120): those versions answer
# with one or two turns at no cost (2.1.100 "Unknown skill: usage", 2.1.113
# "isn't available", 2.1.119 the figures), which judge() reads as COST, so
# the daemon never runs them. There the check is that the version gate sees
# it -- on the `old` cell, 2.1.100. An OK answer below the floor, or a non-OK
# one above it, means USAGE_SINCE is wrong, and fails.
#
# Run from the source tree, not the binary: the binary has no way to run one
# refresh on demand, and the command, judge and gate are the same code.
unset CLAUDE_CONFIG_DIR ANTHROPIC_API_KEY
mkdir -p "$WORK/refresh"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT" py - "$WORK/refresh" <<'EOF' || exit 1
import json, shutil, subprocess, sys
from pc import usage_refresh as ur, wsl

cwd = sys.argv[1]
exe = shutil.which("claude")
try:
    # The daemon's own runner: on Windows, plain run() kills only the
    # claude.cmd shell at the timeout and then waits on node's pipes with no
    # limit, so a stalled run hung the cell instead of failing it.
    r = wsl._bounded_run(ur.command(exe), cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         timeout=ur.RUN_TIMEOUT_S)
except subprocess.TimeoutExpired:
    sys.exit(f"FAIL: claude -p /usage did not finish in {ur.RUN_TIMEOUT_S} s")
verdict = ur.judge(r.returncode, r.stdout or b"")
ver = ur.version_of(exe)
if ver is None:
    sys.exit("FAIL: claude --version gave no version the gate can read"
             " (usage_refresh.parse_version)")
old = ur.version_too_old(ver)
try:
    doc = json.loads(r.stdout or b"")
    keys = sorted(doc) if isinstance(doc, dict) else []
except ValueError:
    keys = None
# Field names only, never values: the answer is Claude Code's own text.
seen = (f"judged {verdict.name}, exit {r.returncode}, keys {keys}"
        if keys is not None else
        f"no JSON (exit {r.returncode}):\n"
        + (r.stderr or b"").decode(errors="replace")[-600:])
here = ".".join(map(str, ver))
floor = ".".join(map(str, ur.USAGE_SINCE))
if verdict is ur.Verdict.OK:
    if old:
        sys.exit(f"FAIL: {here} answers /usage as OK but is below"
                 f" USAGE_SINCE ({floor}): lower the floor.")
    print("  ok   /usage answers the zero-cost JSON result, judged OK")
elif old:
    print(f"  ok   {here} is below USAGE_SINCE ({floor}) and is never run;"
          f" /usage {seen}")
elif verdict is ur.Verdict.COST:
    sys.exit(f"FAIL: logged out, claude -p /usage reads as COST on {here}:"
             " the refresh would turn itself off on every desk. Measure the"
             " command again (CLAUDE.md) before shipping.")
else:
    sys.exit(f"FAIL: claude -p /usage on {here}: {seen}. Every desk would"
             " judge every run FAILED and turn the refresh off a day later;"
             " check judge() against the new answer before shipping.")
EOF

printf 'PASS [%s]\n' "$VERSION"
