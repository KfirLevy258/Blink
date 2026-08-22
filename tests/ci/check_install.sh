#!/bin/sh
# Run the packaged binary for real against one scenario, and check what it did.
#
# The pytest suite covers the same scenarios hermetically, but always with
# CLAUGE_SKIP_DEPS=1 and CLAUGE_SKIP_SERVICE=1 -- so the two steps that touch
# the machine itself, building the virtualenv and registering a login service,
# are exactly the two nothing exercises. That is what this is for, and why CI
# runs it on a real runner of each OS rather than only running pytest.
#
#   tests/ci/check_install.sh <scenario> [work-dir]
#
# Scenarios: no-claude, no-settings, no-statusline, with-statusline,
#            reinstall, foreign-uninstall, spaced-home
#
# CLAUGE_BIN names the binary to test (default: dist/clauge, as built by
# tools/build_binary.sh). CI builds it once per platform and hands the path in.
#
# Set CLAUGE_SKIP_SERVICE=1 to skip the login-service assertions. Do that when
# running this on a machine you care about: the launchd label is a constant, so
# a real run here would bootout whatever agent is already installed and replace
# it with one pointing into this scenario's throwaway HOME.
set -eu

SCENARIO="${1:?usage: check_install.sh <scenario> [work-dir]}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
WORK="${2:-${TMPDIR:-/tmp}/clauge-ci-$SCENARIO}"
BIN="${CLAUGE_BIN:-$ROOT/dist/clauge}"
[ -x "$BIN" ] || { echo "no binary at $BIN -- run tools/build_binary.sh" >&2; exit 1; }

rm -rf "$WORK"
mkdir -p "$WORK"

fail() { printf 'FAIL [%s] %s\n' "$SCENARIO" "$*" >&2; exit 1; }
ok() { printf '  ok   %s\n' "$*"; }

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
BINEXE="clauge"
case "$(uname -s)" in
MINGW* | MSYS* | CYGWIN*)
	NATIVE_HOME=$(cygpath -w "$HOME")
	USERPROFILE="$NATIVE_HOME"
	export USERPROFILE
	BINEXE="clauge.exe"
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

# A file in ~/.clauge that Clauge did not create. Uninstall must never take the
# directory, only its own three files -- the OTA signing key lives here and
# cannot be regenerated.
plant_signing_key() {
	mkdir -p "$HOME/.clauge"
	echo "PRIVATE KEY" >"$HOME/.clauge/ota_signing_key_p256.pem"
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
	# lose a status line Clauge never touched.
	"$BIN" uninstall >"$WORK/out.txt" 2>&1 ||
		fail "uninstall exited non-zero: $(cat "$WORK/out.txt")"
	[ "$(json_get statusLine.command)" = "sh $THEIR_BAR" ] ||
		fail "uninstall clobbered a status line it never installed"
	ok "foreign status line untouched"
	exit 0
fi

"$BIN" >"$WORK/out.txt" 2>&1 || {
	cat "$WORK/out.txt" >&2
	fail "clauge exited non-zero"
}

if [ "$SCENARIO" = "reinstall" ]; then
	"$BIN" >"$WORK/out2.txt" 2>&1 || {
		cat "$WORK/out2.txt" >&2
		fail "second clauge run exited non-zero"
	}
	ok "second run succeeded"
fi

# ------------------------------------------------------------- assertions --

# Disclosure: it asks nothing, so this is the only safeguard. It has to name
# the file, the key and the way back, and do it before the first step runs.
head -n "$(grep -n '^\[1/3\]' "$WORK/out.txt" | head -1 | cut -d: -f1)" \
	"$WORK/out.txt" >"$WORK/disclosure.txt" 2>/dev/null || true
grep -qF "$(native_settings)" "$WORK/disclosure.txt" || fail "disclosure omits settings.json path"
grep -q "statusLine.command" "$WORK/disclosure.txt" || fail "disclosure omits the key"
grep -qF "$BINEXE uninstall" "$WORK/disclosure.txt" ||
	fail "disclosure omits the undo"
ok "disclosure precedes the first step and names file, key, undo"

SHIM="$HOME/.clauge/clauge-statusline.sh"
[ -x "$SHIM" ] || fail "shim not installed at $SHIM"
cmp -s "$SHIM" "$ROOT/tools/clauge-statusline.sh" || fail "installed shim differs from source"
ok "shim installed as a copy, not a pointer into the checkout"

got=$(json_get statusLine.command)
case "$got" in
*"clauge-statusline.sh"*) ;;
*) fail "statusLine.command is '$got'" ;;
esac
case "$got" in
*"$ROOT"*) fail "statusLine points into the checkout: $got" ;;
esac
ok "statusLine.command -> the installed copy"

# The half no unit test reaches: the binary must copy ITSELF somewhere stable
# and be runnable from there, because the login service names that path and
# the customer is told they can delete the download.
[ -x "$HOME/.clauge/bin/$BINEXE" ] || fail "the binary did not install itself"
"$HOME/.clauge/bin/$BINEXE" status >/dev/null || fail "the installed copy does not run"
ok "binary installed itself and runs from ~/.clauge/bin"

if [ "${CLAUGE_SKIP_SERVICE:-0}" != "1" ]; then
	case "$(uname -s)" in
	Darwin)
		[ -f "$HOME/Library/LaunchAgents/com.clauge.bridge.plist" ] ||
			fail "no launchd plist written"
		plutil -lint "$HOME/Library/LaunchAgents/com.clauge.bridge.plist" >/dev/null ||
			fail "launchd plist is not valid"
		ok "launchd plist written and valid"
		;;
	Linux)
		[ -f "$HOME/.config/systemd/user/clauge-bridge.service" ] ||
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
	chain=$(cat "$HOME/.clauge/statusline-chain" 2>/dev/null || echo "")
	[ "$chain" = "sh '$THEIR_BAR'" ] || fail "chain is [$chain], expected [sh '$THEIR_BAR']"
	ok "their command preserved in the chain file"

	# And it actually RUNS. Feeding the shim a payload is the only check that
	# proves the bar still renders rather than merely being recorded.
	rm -f "$HOME/their-bar-ran"
	out=$(printf '%s' '{"rate_limits":{"five_hour":{"used_percentage":11,"resets_at":1},"seven_day":{"used_percentage":22,"resets_at":2}}}' | sh "$SHIM")
	[ -f "$HOME/their-bar-ran" ] || fail "the chained command did not run"
	[ "$out" = "my bar" ] || fail "shim did not pass through their output (got '$out')"
	ok "their bar still renders through the shim"

	[ -s "$HOME/.clauge/statusline.json" ] || fail "shim wrote no payload"
	py -c "
import json,sys
d=json.load(open('$HOME/.clauge/statusline.json'))
assert d['rate_limits']['five_hour']['used_percentage'] == 11, d
" || fail "payload is not what was piped in"
	ok "payload captured for the daemon"
	;;
esac

if [ "$SCENARIO" = "no-statusline" ]; then
	[ "$(json_get model)" = "opus" ] || fail "an unrelated key was lost"
	[ "$(json_get env.FOO)" = "bar" ] || fail "a nested unrelated key was lost"
	ok "unrelated settings keys untouched"
fi

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
[ ! -e "$HOME/.clauge/bin" ] || fail "uninstall left the binary behind"
[ "$(cat "$HOME/.clauge/ota_signing_key_p256.pem")" = "PRIVATE KEY" ] ||
	fail "uninstall destroyed the OTA signing key"
ok "uninstall restored everything and kept the signing key"

printf 'PASS [%s]\n' "$SCENARIO"
