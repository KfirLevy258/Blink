#!/bin/sh
# Run the packaged binary for real against one scenario, and check what it did.
#
# The pytest suite covers the same scenarios hermetically, but always with
# BLINK_SKIP_DEPS=1 and BLINK_SKIP_SERVICE=1 -- so the two steps that touch
# the machine itself, building the virtualenv and registering a login service,
# are exactly the two nothing exercises. That is what this is for, and why CI
# runs it on a real runner of each OS rather than only running pytest.
#
#   tests/ci/check_install.sh <scenario> [work-dir]
#
# Scenarios: no-claude, no-settings, no-statusline, with-statusline,
#            reinstall, foreign-uninstall, spaced-home
#            all-sources, desktop-only, codex-only, broken-data
#
# The last four feed the INSTALLED binary the files the daemon reads on a
# customer's machine -- a status line payload through the shim, hook events
# through the hook shim, a Claude Desktop cache at the platform's path, a
# Codex rollout log -- and check what `blink status` says about each and what
# `blink status --wire` would put on the cable. No board is attached on a
# runner; the wire message is the last thing that can be checked without one.
#
# BLINK_BIN names the binary to test (default: dist/blink, as built by
# tools/build_binary.sh). CI builds it once per platform and hands the path in.
#
# Set BLINK_SKIP_SERVICE=1 to skip the login-service assertions. Do that when
# running this on a machine you care about: the launchd label is a constant, so
# a real run here would bootout whatever agent is already installed and replace
# it with one pointing into this scenario's throwaway HOME.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"
ci_binary

SCENARIO="${1:?usage: check_install.sh <scenario> [work-dir]}"
WORK="${2:-${TMPDIR:-/tmp}/blink-ci-$SCENARIO}"
ci_label "$SCENARIO"

rm -rf "$WORK"
mkdir -p "$WORK"


# ---------------------------------------------------------------- scenario --

case "$SCENARIO" in
spaced-home) HOME="$WORK/a home with spaces" ;;
*) HOME="$WORK/home" ;;
esac
mkdir -p "$HOME"
export HOME

# Windows resolves ~ from USERPROFILE, not HOME, so setting HOME alone left
# the binary installing into the runner's REAL profile while this script
# asserted against a temporary one -- six scenarios failed for that and
# foreign-uninstall PASSED for it, having checked a file nothing had touched.
#
# USERPROFILE also has to be in Windows form: the binary is native Windows
# Python, and it echoes back paths in the shape it was given.
NATIVE_HOME="$HOME"
BINEXE="blink"
case "$(uname -s)" in
MINGW* | MSYS* | CYGWIN*)
	NATIVE_HOME=$(cygpath -w "$HOME")
	USERPROFILE="$NATIVE_HOME"
	export USERPROFILE
	# The Desktop cache lives under %APPDATA%, which the runner points at
	# its real profile. Redirect it into this scenario's HOME -- here, in
	# the main shell, not inside a helper called from a $(...) subshell,
	# where an export dies with the subshell.
	APPDATA="$NATIVE_HOME\\AppData\\Roaming"
	export APPDATA
	BINEXE="blink.exe"
	;;
esac

SEP="/"
case "$(uname -s)" in MINGW* | MSYS* | CYGWIN*) SEP="\\" ;; esac

# The path the binary will print and write, in its own form.
native_settings() {
	printf '%s%s.claude%ssettings.json' "$NATIVE_HOME" "$SEP" "$SEP"
}

# A stand-in for the customer's own status line: it writes a file when run, so
# "their bar still renders" is checked by observing it actually run, not by
# reading the chain file and assuming.
THEIR_BAR="$HOME/.claude/my-bar.sh"
write_their_bar() {
	mkdir -p "$HOME/.claude"
	cat >"$THEIR_BAR" <<EOF
#!/bin/sh
cat >/dev/null
echo "their bar ran" >"$HOME/their-bar-ran"
printf 'my bar'
EOF
	chmod 755 "$THEIR_BAR"
}

settings() { echo "$HOME/.claude/settings.json"; }

write_settings() {
	mkdir -p "$HOME/.claude"
	printf '%s\n' "$1" >"$(settings)"
}

# A file in ~/.blink that Blink did not create. Uninstall must never take the
# directory, only its own three files -- the OTA signing key lives here and
# cannot be regenerated.
plant_signing_key() {
	mkdir -p "$HOME/.blink"
	echo "PRIVATE KEY" >"$HOME/.blink/ota_signing_key_p256.pem"
}

# Defined here as well as below: the scenario setup above the helpers section
# needs it, and a function is only callable after its definition in sh.
py() {
	if command -v python3 >/dev/null 2>&1; then python3 "$@"; else python "$@"; fi
}

# Where Claude Desktop keeps its cache on this platform. APPDATA is set
# explicitly on Windows so the binary and this script agree on the location.
desktop_cache() {
	case "$(uname -s)" in
	Darwin) echo "$HOME/Library/Application Support/Claude/plan-usage-history.json" ;;
	MINGW* | MSYS* | CYGWIN*)
		echo "$HOME/AppData/Roaming/Claude/plan-usage-history.json" ;;
	*) echo "$HOME/.config/Claude/plan-usage-history.json" ;;
	esac
}

# A Desktop cache written a moment ago: four samples over the last half hour,
# session climbing 10 -> 20 %, which is a burn rate of 20 %/h. Real shape
# (tests/fixtures/claude_desktop_plan_usage_history.json), fresh clock.
write_desktop_cache() {
	f=$(desktop_cache)
	mkdir -p "$(dirname "$f")"
	py - "$f" <<-'EOF'
		import json, sys, time
		now = int(time.time() * 1000)
		samples = [{"t": now - back * 1000, "org": "org-ci", "u": {"fh": fh, "sd": 35}}
		           for back, fh in ((1800, 10), (1200, 13.3), (600, 16.7), (0, 20))]
		json.dump({"version": 2, "samples": samples}, open(sys.argv[1], "w"))
	EOF
}

# A Codex rollout log: the real captured tail, with its token_count events
# re-stamped to now and their resets moved into the future.
write_codex_log() {
	d="$HOME/.codex/sessions/2026/08/28"
	mkdir -p "$d"
	py - "$ROOT/tests/fixtures/codex_rollout_tail.jsonl" "$d/rollout-2026-08-28T00-00-00-ci.jsonl" <<-'EOF'
		import json, sys, time, datetime
		now = time.time()
		stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
		with open(sys.argv[1]) as src, open(sys.argv[2], "w") as dst:
		    for line in src:
		        if line.startswith('{"_comment"'):
		            continue
		        o = json.loads(line)
		        p = o.get("payload") or {}
		        if o.get("type") == "event_msg" and p.get("type") == "token_count":
		            o["timestamp"] = stamp
		            rl = p["rate_limits"]
		            rl["primary"] = {"used_percent": 52.0, "window_minutes": 300, "resets_at": int(now) + 5400}
		            rl["secondary"] = {"used_percent": 18.0, "window_minutes": 10080, "resets_at": int(now) + 400000}
		        dst.write(json.dumps(o) + "\n")
	EOF
}

case "$SCENARIO" in
no-claude)
	: ;;                                  # no ~/.claude at all
no-settings)
	mkdir -p "$HOME/.claude" ;;           # directory, no settings.json
no-statusline)
	write_settings '{"model": "opus", "env": {"FOO": "bar"}}' ;;
with-statusline | reinstall | spaced-home)
	write_their_bar
	# Quoted, because a path with spaces has to be -- that is how a real
	# customer's own command would be written, and the point of spaced-home
	# is the SHIM path having spaces, not a command they wrote wrong.
	write_settings "{\"model\": \"opus\", \"statusLine\": {\"type\": \"command\", \"command\": \"sh '$THEIR_BAR'\"}}" ;;
foreign-uninstall)
	write_their_bar
	write_settings "{\"statusLine\": {\"type\": \"command\", \"command\": \"sh $THEIR_BAR\"}}" ;;
all-sources)
	write_settings '{"model": "opus"}'
	write_desktop_cache
	write_codex_log ;;
desktop-only)
	write_desktop_cache ;;                # no ~/.claude, no Codex
codex-only)
	write_codex_log ;;                    # no ~/.claude, no Desktop
broken-data)
	write_settings '{"model": "opus"}'
	f=$(desktop_cache); mkdir -p "$(dirname "$f")"; printf '{not json' >"$f"
	d="$HOME/.codex/sessions/2026/08/28"; mkdir -p "$d"
	printf '{"type":"event_msg","payload":{"type":"token_count","rate_limits":"nonsense"}}\n' \
		>"$d/rollout-2026-08-28T00-00-00-ci.jsonl" ;;
*)
	fail "unknown scenario" ;;
esac
plant_signing_key

printf '== %s (HOME=%s)\n' "$SCENARIO" "$HOME"

# ----------------------------------------------------------------- helpers --

py() {
	# The Windows runners ship `python`, not always `python3`.
	if command -v python3 >/dev/null 2>&1; then python3 "$@"; else python "$@"; fi
}

json_get() {
	py - "$(settings)" "$1" <<-'EOF'
		import json, sys
		try:
		    with open(sys.argv[1]) as f:
		        d = json.load(f)
		except FileNotFoundError:
		    print("")
		    raise SystemExit
		for part in sys.argv[2].split("."):
		    d = (d or {}).get(part) or ""
		print(d if isinstance(d, str) else json.dumps(d))
	EOF
}

# --------------------------------------------------------------------- run --

if [ "$SCENARIO" = "foreign-uninstall" ]; then
	# Never installed. Uninstall must be a no-op on someone else's setup --
	# the case where a person runs it "just to be sure" and would otherwise
	# lose a status line Blink never touched.
	"$BIN" uninstall >"$WORK/out.txt" 2>&1 ||
		fail "uninstall exited non-zero: $(cat "$WORK/out.txt")"
	[ "$(json_get statusLine.command)" = "sh $THEIR_BAR" ] ||
		fail "uninstall clobbered a status line it never installed"
	ok "foreign status line untouched"
	exit 0
fi

"$BIN" >"$WORK/out.txt" 2>&1 || {
	cat "$WORK/out.txt" >&2
	fail "blink exited non-zero"
}

if [ "$SCENARIO" = "reinstall" ]; then
	"$BIN" >"$WORK/out2.txt" 2>&1 || {
		cat "$WORK/out2.txt" >&2
		fail "second blink run exited non-zero"
	}
	ok "second run succeeded"
fi

# ------------------------------------------------------------- assertions --

# Disclosure: it asks nothing, so this is the only safeguard. It has to name
# the file, the key and the way back, and do it before the first step runs.
# '[1/N]', not '[1/3]': the step count changes whenever a step is added, and
# pinning it here made an unrelated feature look like a disclosure regression.
head -n "$(grep -n '^\[1/[0-9]\]' "$WORK/out.txt" | head -1 | cut -d: -f1)" \
	"$WORK/out.txt" >"$WORK/disclosure.txt" 2>/dev/null || true
grep -qF "$(native_settings)" "$WORK/disclosure.txt" || fail "disclosure omits settings.json path"
grep -q "statusLine.command" "$WORK/disclosure.txt" || fail "disclosure omits the key"
# Every key install writes has to be named. This said "statusLine.command, and
# nothing else in the file" for a while after the hooks key started being
# written too -- and install asks nothing, so the disclosure is the only thing
# between us and silently editing a file the customer owns.
grep -q "hooks" "$WORK/disclosure.txt" || fail "disclosure omits the hooks key"
grep -qF "blink-hook.sh" "$WORK/disclosure.txt" || fail "disclosure omits the hook shim"
grep -qF "$BINEXE uninstall" "$WORK/disclosure.txt" ||
	fail "disclosure omits the undo"
ok "disclosure precedes the first step and names file, key, undo"

SHIM="$HOME/.blink/blink-statusline.sh"
[ -x "$SHIM" ] || fail "shim not installed at $SHIM"
cmp -s "$SHIM" "$ROOT/tools/blink-statusline.sh" || fail "installed shim differs from source"
ok "shim installed as a copy, not a pointer into the checkout"

got=$(json_get statusLine.command)
case "$got" in
*"blink-statusline.sh"*) ;;
*) fail "statusLine.command is '$got'" ;;
esac
case "$got" in
*"$ROOT"*) fail "statusLine points into the checkout: $got" ;;
esac
ok "statusLine.command -> the installed copy"

# The half no unit test reaches: the binary must copy ITSELF somewhere stable
# and be runnable from there, because the login service names that path and
# the customer is told they can delete the download.
[ -x "$HOME/.blink/bin/$BINEXE" ] || fail "the binary did not install itself"
"$HOME/.blink/bin/$BINEXE" status >/dev/null || fail "the installed copy does not run"
ok "binary installed itself and runs from ~/.blink/bin"

if [ "${BLINK_SKIP_SERVICE:-0}" != "1" ]; then
	case "$(uname -s)" in
	Darwin)
		[ -f "$HOME/Library/LaunchAgents/com.blink.bridge.plist" ] ||
			fail "no launchd plist written"
		plutil -lint "$HOME/Library/LaunchAgents/com.blink.bridge.plist" >/dev/null ||
			fail "launchd plist is not valid"
		ok "launchd plist written and valid"
		;;
	Linux)
		[ -f "$HOME/.config/systemd/user/blink-bridge.service" ] ||
			fail "no systemd unit written"
		ok "systemd unit written"
		# Deliberately NOT asserting the service is running: a CI runner has
		# no user session bus, so `systemctl --user` cannot start anything.
		# The installer reports that and carries on, which is the behaviour
		# being checked -- it must not fail the install over it.
		;;
	esac
fi

case "$SCENARIO" in
with-statusline | reinstall | spaced-home)
	chain=$(cat "$HOME/.blink/statusline-chain" 2>/dev/null || echo "")
	[ "$chain" = "sh '$THEIR_BAR'" ] || fail "chain is [$chain], expected [sh '$THEIR_BAR']"
	ok "their command preserved in the chain file"

	# And it actually RUNS. Feeding the shim a payload is the only check that
	# proves the bar still renders rather than merely being recorded.
	rm -f "$HOME/their-bar-ran"
	out=$(printf '%s' '{"rate_limits":{"five_hour":{"used_percentage":11,"resets_at":1},"seven_day":{"used_percentage":22,"resets_at":2}}}' | sh "$SHIM")
	[ -f "$HOME/their-bar-ran" ] || fail "the chained command did not run"
	[ "$out" = "my bar" ] || fail "shim did not pass through their output (got '$out')"
	ok "their bar still renders through the shim"

	[ -s "$HOME/.blink/statusline.json" ] || fail "shim wrote no payload"
	# The path goes in as an ARGUMENT, not inside the source. Under Git Bash a
	# POSIX path handed to a native Windows program is auto-converted to
	# Windows form; a path embedded in a string is not, so the identical check
	# passed for json_get and failed here.
	py - "$HOME/.blink/statusline.json" <<-'EOF' || fail "payload is not what was piped in"
		import json, sys
		d = json.load(open(sys.argv[1]))
		assert d["rate_limits"]["five_hour"]["used_percentage"] == 11, d
	EOF
	ok "payload captured for the daemon"
	;;
esac

if [ "$SCENARIO" = "no-statusline" ]; then
	[ "$(json_get model)" = "opus" ] || fail "an unrelated key was lost"
	[ "$(json_get env.FOO)" = "bar" ] || fail "a nested unrelated key was lost"
	ok "unrelated settings keys untouched"
fi

# -------------------------------------------------------- the data paths --
#
# What the daemon reads, fed through the installed pieces the way the real
# programs feed them, then read back through the installed binary.

HOOK="$HOME/.blink/blink-hook.sh"
wire() {
	"$HOME/.blink/bin/$BINEXE" status --wire 2>/dev/null | grep '^{' | head -1
}
wire_get() {
	# wire_get <key> -> the value, or "" when absent. A python one-liner
	# rather than grep: the message is JSON and keys can be prefixes of
	# one another (session_pct / p2_session_pct).
	printf '%s' "$1" | py -c 'import json,sys; d=json.load(sys.stdin); v=d.get(sys.argv[1], ""); print(v if isinstance(v,str) else json.dumps(v))' "$2"
}

case "$SCENARIO" in
all-sources)
	# Claude Code: a status line render, then two sessions' worth of hooks.
	printf '%s' '{"session_id":"ci-1","cwd":"/x","rate_limits":{"five_hour":{"used_percentage":37,"resets_at":'"$(($(date +%s) + 7200))"'},"seven_day":{"used_percentage":12,"resets_at":'"$(($(date +%s) + 500000))"'}}}' | sh "$SHIM" >/dev/null
	printf '{"session_id":"ci-1"}' | sh "$HOOK" PreToolUse
	printf '{"session_id":"ci-2"}' | sh "$HOOK" Stop
	printf '{"session_id":"ci-1","agent_id":"a1"}' | sh "$HOOK" SubagentStart

	st=$("$HOME/.blink/bin/$BINEXE" status 2>&1) || fail "status exited non-zero: $st"
	echo "$st" | grep -q "Usage data  fresh" || fail "status does not see the status line payload: $st"
	echo "$st" | grep -q "2 live sessions" || fail "status does not count the two sessions: $st"
	echo "$st" | grep -q "Desktop     usage cache parsed" || fail "status did not parse the Desktop cache: $st"
	echo "$st" | grep -q "Codex       session log parsed" || fail "status did not parse the Codex log: $st"
	ok "status sees all three sources and both sessions"

	w=$(wire); [ -n "$w" ] || fail "no wire message"
	[ "$(wire_get "$w" provider)" = "claude" ] || fail "primary is not claude: $w"
	[ "$(wire_get "$w" src)" = "cli" ] || fail "primary source is not the status line: $w"
	[ "$(wire_get "$w" session_pct)" = "37.0" ] || fail "session_pct wrong: $w"
	[ "$(wire_get "$w" weekly_pct)" = "12.0" ] || fail "weekly_pct wrong: $w"
	[ "$(wire_get "$w" state)" = "running" ] || fail "state should be running (worst of running+idle): $w"
	[ "$(wire_get "$w" n_sess)" = "2" ] || fail "n_sess wrong: $w"
	[ "$(wire_get "$w" n_agents)" = "1" ] || fail "n_agents wrong: $w"
	[ "$(wire_get "$w" p2)" = "codex" ] || fail "second provider is not codex: $w"
	[ "$(wire_get "$w" p2_session_pct)" = "52.0" ] || fail "p2_session_pct wrong: $w"
	[ "$(wire_get "$w" p2_stale)" = "false" ] || fail "codex reading should be fresh: $w"
	[ -z "$(wire_get "$w" burn_pph)" ] || fail "a burn rate must not be sent when a reset time exists: $w"
	[ "${#w}" -le 512 ] || fail "wire message over the board's 512-byte line: ${#w}"
	ok "wire: claude (status line) primary with state and counts, codex secondary, under budget"
	;;
desktop-only)
	st=$("$HOME/.blink/bin/$BINEXE" status 2>&1) || fail "status exited non-zero: $st"
	echo "$st" | grep -q "Desktop     usage cache parsed" || fail "status did not parse the Desktop cache: $st"
	echo "$st" | grep -q "Codex       no session logs" || fail "status should report no Codex: $st"
	# The "alone" wording keys off Claude Code being absent from PATH, which
	# is true of every CI runner and false of a developer's machine.
	if command -v claude >/dev/null 2>&1; then
		ok "status parses the Desktop cache (claude is on PATH here, so the 'alone' wording is asserted on runners only)"
	else
		echo "$st" | grep -q "Claude Desktop alone" || fail "status does not say it runs on Desktop alone: $st"
		grep -q "runs on" "$WORK/out.txt" || fail "install did not tell a Desktop-only user what the panel shows"
		ok "status and install both explain the Desktop-only panel"
	fi

	w=$(wire); [ -n "$w" ] || fail "no wire message"
	[ "$(wire_get "$w" src)" = "desktop" ] || fail "source is not the Desktop cache: $w"
	[ "$(wire_get "$w" session_pct)" = "20.0" ] || fail "session_pct wrong: $w"
	[ "$(wire_get "$w" session_resets_in_s)" = "-1" ] || fail "Desktop must not claim a reset time: $w"
	b=$(wire_get "$w" burn_pph); [ -n "$b" ] || fail "no burn rate on a Desktop-only machine: $w"
	py -c 'import sys; b=float(sys.argv[1]); sys.exit(0 if 19 <= b <= 21 else 1)' "$b" || fail "burn rate should be ~20 %/h, got $b"
	[ -z "$(wire_get "$w" p2)" ] || fail "no second provider expected: $w"
	ok "wire: desktop percentages, no reset time, a 20 %/h burn rate"
	;;
codex-only)
	st=$("$HOME/.blink/bin/$BINEXE" status 2>&1) || fail "status exited non-zero: $st"
	echo "$st" | grep -q "Codex       session log parsed" || fail "status did not parse the Codex log: $st"
	if command -v claude >/dev/null 2>&1; then
		ok "status parses the Codex log (claude is on PATH here, so the 'alone' wording is asserted on runners only)"
	else
		echo "$st" | grep -q "running on Codex alone" || fail "status does not say it runs on Codex alone: $st"
		grep -q "runs on Codex" "$WORK/out.txt" || fail "install did not tell a Codex-only user what the panel shows"
		ok "status and install both explain the Codex-only panel"
	fi

	w=$(wire); [ -n "$w" ] || fail "no wire message"
	[ "$(wire_get "$w" provider)" = "codex" ] || fail "primary is not codex: $w"
	[ "$(wire_get "$w" session_pct)" = "52.0" ] || fail "session_pct wrong: $w"
	[ "$(wire_get "$w" weekly_pct)" = "18.0" ] || fail "weekly_pct wrong: $w"
	r=$(wire_get "$w" session_resets_in_s); [ "$r" -gt 0 ] || fail "codex reset countdown missing: $w"
	ok "wire: codex primary with both countdowns"
	;;
broken-data)
	st=$("$HOME/.blink/bin/$BINEXE" status 2>&1) || fail "status exited non-zero on broken files: $st"
	echo "$st" | grep -q "did not parse" || fail "status does not report the unparseable Desktop cache: $st"
	echo "$st" | grep -q "none with a rate-limit line" || fail "status does not report the useless Codex log: $st"
	ok "status names both broken sources and exits 0"
	w=$(wire)
	[ -z "$w" ] || fail "nothing should reach the wire from two broken files, got: $w"
	ok "nothing invented for the wire"
	;;
esac

# ----------------------------------------------------------------- undo --

"$BIN" uninstall >"$WORK/undo.txt" 2>&1 || {
	cat "$WORK/undo.txt" >&2
	fail "uninstall exited non-zero"
}

case "$SCENARIO" in
with-statusline | reinstall | spaced-home)
	[ "$(json_get statusLine.command)" = "sh '$THEIR_BAR'" ] ||
		fail "uninstall did not restore their command" ;;
*)
	[ "$(json_get statusLine.command)" = "" ] ||
		fail "uninstall left a statusLine behind" ;;
esac
[ ! -e "$SHIM" ] || fail "uninstall left the shim behind"

# Windows cannot delete a running executable, and the uninstaller usually IS
# the executable -- the undo hint we print names the installed copy. So on
# Windows the removal is handed to a detached cmd that waits for this process
# to exit. Wait for it here rather than asserting instantly; on the other two
# platforms the directory is already gone and this loop ends immediately.
n=0
while [ -e "$HOME/.blink/bin" ] && [ "$n" -lt 30 ]; do
	sleep 1
	n=$((n + 1))
done
if [ -e "$HOME/.blink/bin" ]; then
	# Say WHO is holding it. Three rounds were spent guessing at this from a
	# bare "left the binary behind", and the answer -- which process, and
	# whether the task was still registered -- was never in the log.
	case "$(uname -s)" in
	MINGW* | MSYS* | CYGWIN*)
		echo "--- processes still running the binary ---" >&2
		tasklist //fi "IMAGENAME eq blink.exe" //v >&2 || true
		echo "--- scheduled task ---" >&2
		schtasks //query //tn "Blink bridge" >&2 || true
		echo "--- what is in the directory ---" >&2
		ls -l "$HOME/.blink/bin" >&2 || true
		;;
	esac
	fail "uninstall left the binary behind (waited ${n}s)"
fi
[ "$(cat "$HOME/.blink/ota_signing_key_p256.pem")" = "PRIVATE KEY" ] ||
	fail "uninstall destroyed the OTA signing key"
ok "uninstall restored everything and kept the signing key"

printf 'PASS [%s]\n' "$SCENARIO"
