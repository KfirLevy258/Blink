#!/bin/sh
# Clauge setup -- one command, no questions.
#
#   ./install.sh            set everything up and start the bridge
#   ./install.sh uninstall  put it all back
#   ./install.sh status     is the panel getting data?
#
# Plugging the board in is meant to be the whole setup, so this script asks
# nothing -- not even "is that OK?". That is exactly what makes the disclosure
# below non-optional: it names every file it will create or change BEFORE it
# changes any of them, and prints even when stdout is redirected, so there is
# always a record of what happened. Same reasoning as pc/install_statusline.py's
# _announce(); this one covers the three things outside that module's scope.
#
# Everything it does is reversible with `./install.sh uninstall`.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
CLAUGE_HOME="$HOME/.clauge"
VENV="$CLAUGE_HOME/venv"
SHIM_SRC="$ROOT/tools/clauge-statusline.sh"
# The shim is COPIED here rather than pointed at inside the checkout. What goes
# into settings.json is an absolute path that Claude Code runs on every render,
# and a customer who moves, renames, or deletes the repo would otherwise turn
# their status line into a "No such file or directory" on every prompt. The
# shim only ever touches $HOME, so it is self-contained wherever it lives.
SHIM="$CLAUGE_HOME/clauge-statusline.sh"
# The daemon is copied here too, for the same reason as the shim: the login
# service names an absolute path and runs it at every login, so pointing it
# into the download would mean the customer can never move or delete what
# they unpacked. They should be able to throw it away the moment it is done.
APP="$CLAUGE_HOME/app"
LOG="$CLAUGE_HOME/bridge.log"
SETTINGS="$HOME/.claude/settings.json"
LABEL="com.clauge.bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/clauge-bridge.service"

# Test hooks. Both default to off; they exist so the installer can be exercised
# end-to-end under a temporary HOME without reaching the network or registering
# a real login service on the machine running the tests.
SKIP_DEPS="${CLAUGE_SKIP_DEPS:-0}"
SKIP_SERVICE="${CLAUGE_SKIP_SERVICE:-0}"

OS=$(uname -s)

# The oldest Claude Code that carries usage figures in its status line
# payload. 2.1.0 does not carry rate_limits at all; 2.1.100 does. Below this,
# every part of the install succeeds and the panel stays blank forever --
# which is why this is checked here and not only stated in the README.
MIN_CLAUDE="2.1.100"

die() { printf '%s\n' "$@" >&2; exit 1; }

find_python() {
	for c in python3 python; do
		if command -v "$c" >/dev/null 2>&1 &&
			"$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
			command -v "$c"
			return 0
		fi
	done
	die "Clauge needs Python 3.9 or newer, and I could not find it." \
		"  macOS:  brew install python3   (or install Xcode command line tools)" \
		"  Linux:  your package manager's python3"
}

claude_version() {
	command -v claude >/dev/null 2>&1 || return 1
	# `claude --version` prints e.g. "2.1.229 (Claude Code)".
	claude --version 2>/dev/null | sed -n 's/^\([0-9][0-9.]*\).*/\1/p'
}

claude_is_new_enough() {
	"$PY" - "$1" "$MIN_CLAUDE" <<-'EOF'
		import sys

		def parts(v):
		    out = []
		    for piece in v.split(".")[:3]:
		        out.append(int(piece) if piece.isdigit() else 0)
		    while len(out) < 3:
		        out.append(0)
		    return tuple(out)

		sys.exit(0 if parts(sys.argv[1]) >= parts(sys.argv[2]) else 1)
	EOF
}

check_claude_version() {
	v=$(claude_version) || return 0     # not on PATH; nothing to compare
	[ -n "$v" ] || return 0             # unrecognised output; do not guess
	claude_is_new_enough "$v" && return 0

	# A warning, not a refusal. Everything installed here is correct and
	# stays correct: the moment they update Claude Code the panel starts
	# working, with nothing to redo. Refusing would make them run this
	# again for no reason.
	echo
	echo "  !! Your Claude Code is $v, and Clauge needs $MIN_CLAUDE or newer."
	echo "     Older versions do not put the usage figures in the status line"
	echo "     at all, so the panel will sit blank until you update:"
	echo
	echo "       npm install -g @anthropic-ai/claude-code@latest"
	echo
	echo "     Nothing else needs redoing -- it starts working on its own."
}

# --------------------------------------------------------------------------
# Disclosure
# --------------------------------------------------------------------------

announce() {
	previous=""
	if [ -f "$SETTINGS" ]; then
		previous=$("$PY" - "$SETTINGS" <<-'EOF' 2>/dev/null || true
			import json, sys
			try:
			    with open(sys.argv[1]) as f:
			        print((json.load(f).get("statusLine") or {}).get("command", ""))
			except Exception:
			    pass
		EOF
		)
	fi

	echo "Clauge setup. Here is everything it is about to do, before it does any of it."
	echo
	echo "  Creates    $VENV"
	echo "             a private Python environment for the bridge (pyserial,"
	echo "             esptool). Your system Python is not modified."
	echo "  Creates    $SHIM"
	echo "  Creates    $APP"
	echo "             copies of the status line shim and the bridge, so this"
	echo "             folder can be deleted once setup is done."
	echo "  Changes    $SETTINGS"
	echo "             the statusLine.command key, and nothing else in the file."
	if [ -n "$previous" ]; then
		echo "             Your current command is recorded and still runs, so your"
		echo "             bar renders exactly as before:"
		echo "               $previous"
	fi
	case "$OS" in
	Darwin) echo "  Creates    $PLIST" ;;
	Linux) echo "  Creates    $UNIT" ;;
	esac
	echo "             so the bridge starts when you log in and reconnects on"
	echo "             its own when the board is plugged in."
	echo
	echo "  It reads or stores nothing else -- no credential, no token, no"
	echo "  account data. The usage figures come from Claude Code, which has"
	echo "  already worked them out."
	echo
	echo "  To undo all of it:  $ROOT/install.sh uninstall"
	echo
}

# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

install_deps() {
	if [ "$SKIP_DEPS" = "1" ]; then
		echo "[1/4] Python environment ... skipped (CLAUGE_SKIP_DEPS=1)"
		return 0
	fi
	printf '[1/4] Python environment ... '
	mkdir -p "$CLAUGE_HOME"
	if [ ! -x "$VENV/bin/python" ]; then
		# A venv, not `pip install --user`: recent macOS and Debian Pythons are
		# marked externally-managed (PEP 668) and refuse a --user install
		# outright, which would fail this script on a stock machine.
		"$PY" -m venv "$VENV" >/dev/null 2>&1 ||
			die "Could not create a Python environment at $VENV." \
				"  Debian/Ubuntu: sudo apt install python3-venv, then rerun."
	fi
	# pyserial talks to the board; esptool is how the bridge updates it. Both
	# pinned in pc/requirements.txt -- see the reasoning there. Unpinned, each
	# customer gets whatever PyPI serves that day.
	"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check \
		-r "$ROOT/pc/requirements.txt" >/dev/null 2>&1 ||
		die "Could not install pyserial and esptool into $VENV." \
			"  Check your network connection and rerun; nothing has been changed yet."
	echo "ok"
}

install_shim() {
	printf '[2/4] Status line shim and daemon ... '
	[ -f "$SHIM_SRC" ] || die "Missing $SHIM_SRC -- run this from the Clauge folder."
	mkdir -p "$CLAUGE_HOME"
	cp "$SHIM_SRC" "$SHIM"
	chmod 755 "$SHIM"

	# Replaced wholesale rather than merged: a leftover .py from an older
	# version sitting beside a newer one is an import that silently wins.
	rm -rf "$APP"
	mkdir -p "$APP/pc"
	cp "$ROOT/claude_usage_bridge.py" "$APP/"
	cp "$ROOT"/pc/*.py "$APP/pc/"
	cp "$ROOT/pc/requirements.txt" "$APP/pc/"
	echo "$CLAUGE_HOME"
}

install_statusline() {
	echo "[3/4] Claude Code setting:"
	# Captured rather than piped straight into sed. A pipeline's exit status is
	# the LAST command's, so `python ... | sed` reports sed's success even when
	# the install raised -- which printed a traceback and then "Done." under a
	# HOME where ~/.claude did not exist. Capturing keeps the status, and keeps
	# stderr in order with stdout instead of racing a block-buffered pipe.
	# --shim pins the copy above, so this stays correct across reinstalls from a
	# checkout at a different path: the command text never changes, which is what
	# install()'s stateless self-recognition compares against.
	if out=$(cd "$ROOT" && "$PY" -m pc.install_statusline --shim "$SHIM" \
		--undo-hint "$ROOT/install.sh uninstall" install 2>&1); then
		printf '%s\n' "$out" | sed 's/^/      /'
	else
		printf '%s\n' "$out" | sed 's/^/      /' >&2
		die "" "Could not change the Claude Code setting -- stopping here." \
			"Nothing else was started; $SHIM is safe to delete."
	fi
}

install_service() {
	if [ "$SKIP_SERVICE" = "1" ]; then
		echo "[4/4] Background service ... skipped (CLAUGE_SKIP_SERVICE=1)"
		return 0
	fi
	printf '[4/4] Background service ... '
	case "$OS" in
	Darwin) install_launchd ;;
	Linux) install_systemd ;;
	*)
		echo "not supported on $OS"
		echo "      Start the bridge by hand when you want it:"
		echo "        $VENV/bin/python $APP/claude_usage_bridge.py"
		;;
	esac
}

xml_escape() {
	printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

install_launchd() {
	mkdir -p "$(dirname "$PLIST")"
	cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$VENV/bin/python")</string>
    <string>-u</string>
    <string>$(xml_escape "$APP/claude_usage_bridge.py")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$APP")</string>
  <key>RunAtLoad</key><true/>
  <!-- The bridge exits when no board is attached, so KeepAlive is what makes
       "plug it in and it works" true: launchd restarts it, throttled to one
       attempt every 10 s, and it picks the board up on the next try. -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$(xml_escape "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG")</string>
</dict>
</plist>
EOF
	uid=$(id -u)
	# bootout first so a rerun replaces the running agent instead of failing
	# with "service already loaded".
	launchctl bootout "gui/$uid/$LABEL" >/dev/null 2>&1 || true
	if launchctl bootstrap "gui/$uid" "$PLIST" >/dev/null 2>&1; then
		echo "running (launchd: $LABEL)"
	elif launchctl load -w "$PLIST" >/dev/null 2>&1; then
		echo "running (launchd: $LABEL)"
	else
		echo "installed, but could not be started"
		echo "      Start it by hand:  launchctl bootstrap gui/$uid \"$PLIST\""
	fi
}

install_systemd() {
	if ! command -v systemctl >/dev/null 2>&1; then
		echo "no systemd here"
		echo "      Start the bridge by hand when you want it:"
		echo "        $VENV/bin/python $APP/claude_usage_bridge.py"
		return 0
	fi
	mkdir -p "$UNIT_DIR"
	cat >"$UNIT" <<EOF
[Unit]
Description=Clauge USB bridge

[Service]
# Restart=always for the same reason as KeepAlive on macOS: the bridge exits
# when no board is attached, and this is what makes plugging one in enough.
ExecStart=$VENV/bin/python -u $APP/claude_usage_bridge.py
WorkingDirectory=$APP
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
	systemctl --user daemon-reload >/dev/null 2>&1 || true
	if systemctl --user enable --now clauge-bridge.service >/dev/null 2>&1; then
		echo "running (systemd: clauge-bridge)"
	else
		echo "installed, but could not be started"
		echo "      Start it by hand:  systemctl --user enable --now clauge-bridge"
	fi
	if command -v getent >/dev/null 2>&1 &&
		! id -nG 2>/dev/null | tr ' ' '\n' | grep -qx -e dialout -e uucp; then
		echo "      Note: reading the board's serial port usually needs group"
		echo "      membership -- sudo usermod -aG dialout \$USER, then log back in."
	fi
}

# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

do_install() {
	[ -f "$ROOT/claude_usage_bridge.py" ] ||
		die "Run this from the Clauge folder (no claude_usage_bridge.py next to $0)."
	announce
	install_deps
	install_shim
	install_statusline
	install_service
	echo
	echo "Done. Plug the board in over USB -- it picks it up on its own."
	# Last, so it is the thing left on screen rather than scrolled past.
	check_claude_version
	case "$OS" in
	Darwin) echo "  Log:     $LOG" ;;
	Linux) echo "  Log:     journalctl --user -u clauge-bridge -f" ;;
	esac
	echo "  Check:   $ROOT/install.sh status"
	echo "  Undo:    $ROOT/install.sh uninstall"
}

do_uninstall() {
	echo "Clauge uninstall."
	echo

	printf '[1/3] Background service ... '
	# Honour the same skip install does. Not cosmetic symmetry: the launchd
	# label and the systemd unit name are CONSTANTS, while everything else
	# here is scoped to $HOME. So an uninstall run under a throwaway HOME --
	# a test, a CI scenario -- still boots out the real agent belonging to
	# whoever is logged in, and the board on their desk goes to HOST LOST
	# 35 seconds later. Observed exactly that way.
	if [ "$SKIP_SERVICE" = "1" ]; then
		echo "skipped (CLAUGE_SKIP_SERVICE=1)"
	else
	case "$OS" in
	Darwin)
		launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 ||
			launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
		rm -f "$PLIST"
		echo "removed"
		;;
	Linux)
		if command -v systemctl >/dev/null 2>&1; then
			systemctl --user disable --now clauge-bridge.service >/dev/null 2>&1 || true
		fi
		rm -f "$UNIT"
		# An if, not `A && B || true`: in that form the `|| true` also swallows
		# a failure of the test itself, so it reads as "ignore daemon-reload
		# errors" while actually meaning something broader. shellcheck SC2015.
		if command -v systemctl >/dev/null 2>&1; then
			systemctl --user daemon-reload >/dev/null 2>&1 || true
		fi
		echo "removed"
		;;
	*) echo "nothing to remove" ;;
	esac
	fi

	# Before the shim is deleted: uninstall() identifies its own command by that
	# exact path, and it restores the customer's original status line. Deleting
	# the file first would not break it, but ordering it this way keeps the undo
	# in the same sequence as the install, in reverse.
	echo "[2/3] Claude Code setting:"
	# Captured, not piped -- same reason as install_statusline(). Here a failure
	# is not fatal: the rest of the undo should still run, but it must be said
	# out loud rather than swallowed by sed's exit status.
	if out=$(cd "$ROOT" && "$PY" -m pc.install_statusline --shim "$SHIM" uninstall 2>&1); then
		printf '%s\n' "$out" | sed 's/^/      /'
	else
		printf '%s\n' "$out" | sed 's/^/      /' >&2
		echo "      Could not restore the setting -- check $SETTINGS by hand." >&2
	fi

	printf '[3/3] Files ... '
	# Only the three things install created. NOT $CLAUGE_HOME itself: it also
	# holds the OTA signing key (~/.clauge/ota_signing_key_p256.pem), which is
	# not ours to delete and cannot be regenerated -- every board already flashed
	# with its public half would stop accepting updates.
	rm -rf "$VENV" "$APP"
	rm -f "$SHIM" "$CLAUGE_HOME/statusline.json" "$CLAUGE_HOME/statusline.json.tmp"
	echo "removed"
	echo
	echo "Done. Nothing of Clauge's is left running."
}

do_status() {
	printf 'Bridge      '
	case "$OS" in
	Darwin)
		if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
			echo "registered with launchd"
		else
			echo "not installed"
		fi
		;;
	Linux)
		if command -v systemctl >/dev/null 2>&1 &&
			systemctl --user is-active --quiet clauge-bridge.service; then
			echo "running"
		else
			echo "not running"
		fi
		;;
	*) echo "unknown on $OS" ;;
	esac

	# Where someone debugging a blank panel will look, so the floor belongs
	# here too, not only at install time.
	printf 'Claude Code '
	cv=$(claude_version) || cv=""
	if [ -z "$cv" ]; then
		echo "not found on PATH"
	elif claude_is_new_enough "$cv"; then
		echo "$cv"
	else
		echo "$cv -- TOO OLD, needs $MIN_CLAUDE+ (panel will stay blank)"
	fi

	printf 'Status line '
	if [ -f "$SHIM" ]; then echo "installed at $SHIM"; else echo "not installed"; fi

	# The single most useful support answer: is fresh data actually arriving?
	# A missing or old payload means Claude Code has not rendered its status
	# line recently, which is the usual cause of a panel showing stale figures.
	printf 'Usage data  '
	if [ -f "$CLAUGE_HOME/statusline.json" ]; then
		age=$("$PY" - "$CLAUGE_HOME/statusline.json" <<-'EOF'
			import os, sys, time
			print(int(time.time() - os.path.getmtime(sys.argv[1])))
		EOF
		)
		if [ "$age" -lt 120 ]; then
			echo "fresh (${age}s old)"
		else
			echo "stale (${age}s old) -- open Claude Code to refresh it"
		fi
	else
		echo "none yet -- open Claude Code once so it renders its status line"
	fi
}

PY=$(find_python)

case "${1:-install}" in
install) do_install ;;
uninstall) do_uninstall ;;
status) do_status ;;
-h | --help | help)
	echo "usage: $0 [install|uninstall|status]"
	;;
*) die "usage: $0 [install|uninstall|status]" ;;
esac
