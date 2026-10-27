# Find the Zephyr workspace instead of naming one. Sourced, not run.
#
# This used to be spelled "$HOME/zephyr-v4.4.0" in four scripts, and that path
# was wrong twice over. The workspace on the machine those scripts were written
# on was ~/zephyrproject, so the literal never resolved there either -- burn.sh
# only survived because its `. activate` is followed by `|| true`, and
# release.sh would have failed outright the first time anyone ran it from a
# clean shell.
#
# The second way it was wrong is worse: Zephyr 4.4.0 CANNOT BUILD THIS
# FIRMWARE. Measured 2026-09-09 -- its mbedTLS is 4.1.0, which removed the
# legacy mbedtls/sha256.h that src/ota.c:21 includes (there are no sha headers
# in that module at all), and the LVGL it pins moved `gesture_limit` out of the
# public lv_indev headers, which src/ui_settings.c:2189 reads. Zephyr 4.3.1 has
# both -- mbedTLS 3.6.6 and gesture_limit at lv_indev_private.h:69 -- and wants
# SDK 0.17.4 rather than the 1.0.1 that 4.4.0 asks for.
#
# So a directory name that carries a version number is a claim this repo cannot
# keep. Searching for a workspace costs nothing and does not go stale.
#
# BLINK_ZEPHYR wins if it is set, for a machine that keeps several. Otherwise
# the first candidate that looks like a west workspace is taken, and "looks
# like" means it holds the venv these scripts are about to source -- not merely
# that a directory of that name exists. A half-finished checkout should read as
# absent here, not as a workspace that then fails deep inside a build.
#
# zsh treats a glob that matches nothing as an error and abandons the whole
# command, so on a Mac holding only one of the four candidates the documented
# `. tools/lib_zephyr.sh && blink_zephyr_activate` printed "no matches found:
# ~/zephyr-v*" and did nothing else. Found by gapietro in their fork
# (2026-10-12), and hit on this repo's own Mac on 2026-10-25. bash does the
# same when the caller has `shopt -s failglob` on. So for the length of the
# search only, neither is allowed to abort: zsh gets nullglob through
# localoptions, which drops an unmatched pattern from the list altogether,
# and bash has failglob switched off and on again around the call, which
# leaves the pattern as its literal text the way plain sh does, for the -f
# test below to reject. Neither branch runs under sh or dash.
blink_zephyr_ws() {
	if [ -n "${ZSH_VERSION:-}" ]; then
		setopt localoptions nullglob
	fi
	_bz_fg=""
	if [ -n "${BASH_VERSION:-}" ] && shopt -q failglob 2>/dev/null; then
		shopt -u failglob
		_bz_fg=1
	fi
	# In an if, so a `set -e` caller cannot exit before failglob is restored.
	if _blink_zephyr_ws_find; then _bz_rc=0; else _bz_rc=$?; fi
	if [ -n "$_bz_fg" ]; then
		shopt -s failglob
	fi
	return "$_bz_rc"
}

_blink_zephyr_ws_find() {
	if [ -n "${BLINK_ZEPHYR:-}" ]; then
		[ -f "$BLINK_ZEPHYR/.venv/bin/activate" ] || return 1
		printf '%s\n' "$BLINK_ZEPHYR"
		return 0
	fi
	for _bz_d in "$HOME"/zephyrproject "$HOME"/zephyr-v* \
	             "$HOME"/Projects/zephyrproject "$HOME"/Projects/zephyr-v*; do
		[ -f "$_bz_d/.venv/bin/activate" ] || continue
		[ -d "$_bz_d/zephyr" ] || continue
		printf '%s\n' "$_bz_d"
		return 0
	done
	return 1
}

# Put a Zephyr build environment in this shell, or say why it cannot.
#
# ZEPHYR_TOOLCHAIN_VARIANT has to be exported before the build: Zephyr 4.3.1's
# cmake/modules/FindZephyr-sdk.cmake:57 uses it unquoted inside an if(), so
# when it is unset CMake 4.4.3 -- what Homebrew ships as of 2026-09 -- sees an
# empty operand and fails the configure with "Unknown arguments specified".
# Older CMake tolerated the empty expansion, which is why this appears only on
# newly set-up machines and never on one that has been building for a year.
blink_zephyr_activate() {
	_bz_ws=$(blink_zephyr_ws) || {
		echo "FATAL: no Zephyr workspace found." >&2
		echo "       Looked for a directory holding .venv/bin/activate and zephyr/ under" >&2
		echo "       ~/zephyrproject, ~/zephyr-v*, ~/Projects/zephyrproject, ~/Projects/zephyr-v*." >&2
		echo "       Set BLINK_ZEPHYR to the workspace, or see firmware/README.md." >&2
		return 1
	}
	# shellcheck disable=SC1090
	. "$_bz_ws/.venv/bin/activate"
	ZEPHYR_BASE="${ZEPHYR_BASE:-$_bz_ws/zephyr}"
	export ZEPHYR_BASE
	export ZEPHYR_TOOLCHAIN_VARIANT="${ZEPHYR_TOOLCHAIN_VARIANT:-zephyr}"
	# Kept because release.sh sourced it before this helper existed. Exporting
	# ZEPHYR_BASE is enough for `west build` on its own; this also puts
	# zephyr/scripts on PATH, and dropping it silently would be a behaviour
	# change buried in a refactor.
	# shellcheck disable=SC1090
	. "$_bz_ws/zephyr/zephyr-env.sh" >/dev/null 2>&1 || true
}

# Where this file lives, so the pip hint below names this checkout's pins however
# it was sourced. ROOT and the current directory are only guesses: from an
# interactive shell outside the repo neither is the repo, and the fallback
# that used to print then was a pip line with no versions in it -- pasted into
# bash it installed bitstring 5.0.0, and zsh handed its trailing `#` comment
# to pip as an argument. bash and zsh can each say which file is being
# sourced; plain POSIX sh cannot, so dash falls back to ROOT. The expansions
# sit inside eval because dash rejects their syntax outright, and only
# builtins are used because a caller's PATH may hold nothing at all.
_bz_self=""
if [ -n "${BASH_VERSION:-}" ]; then
	eval '_bz_self=${BASH_SOURCE[0]:-}'
elif [ -n "${ZSH_VERSION:-}" ]; then
	eval '_bz_self=${(%):-%x}'
fi
_bz_repo=""
# cd's own output goes nowhere: an interactive zsh runs the user's chpwd hooks
# on every cd, and one that lists the new directory (this repo's own Mac has
# one) landed in this capture ahead of pwd's line, so _bz_repo became a
# directory listing and the pinned hint was never printed (2026-10-25).
case "$_bz_self" in
	*/*) _bz_repo=$(CDPATH='' cd -- "${_bz_self%/*}/.." >/dev/null 2>&1 && pwd) || _bz_repo="" ;;
	?*) _bz_repo=$(CDPATH='' cd -- .. >/dev/null 2>&1 && pwd) || _bz_repo="" ;;
esac

# The command that installs esptool and bitstring at this repo's pins into the
# interpreter named by $1, on one line, or a failure status when no
# pc/requirements.txt can be found. `-c` makes the file a constraints list:
# only the two packages named are installed, each at the version it pins, so
# the versions are never written out a second time where they could drift.
# The path is absolute so the line works pasted from any directory. burn.sh
# prints the same line for its own interpreter. Builtins only.
blink_zephyr_pip_hint() {
	_bz_req=""
	for _bz_try in "$_bz_repo" "${ROOT:-}" "$PWD"; do
		if [ -n "$_bz_try" ] && [ -r "$_bz_try/pc/requirements.txt" ]; then
			_bz_req="$_bz_try/pc/requirements.txt"
			break
		fi
	done
	[ -n "$_bz_req" ] || return 1
	case "$_bz_req" in
		/*) ;;
		*) _bz_req="$PWD/$_bz_req" ;;
	esac
	printf '%s -m pip install -c %s esptool bitstring\n' "$1" "$_bz_req"
}

# Can the activated workspace build this firmware? Called only right before a
# `west build` -- by release.sh, burn.sh and `dev.sh flash` -- and never from
# blink_zephyr_activate: `dev.sh up` activates to relaunch the daemon after
# `down` has killed it, and an activate that failed here would leave the desk
# on HOST LOST for want of a tool the daemon never runs.
#
# It asks one thing: is esptool on PATH. Zephyr 4.3.1's ESP32 configure looks
# for it (soc/espressif/common/CMakeLists.txt) and stops without it, telling
# you to run `west packages pip --install`. gapietro's fork hit a missing
# esptool cutting a release on 2026-10-13. That Zephyr command asks only for
# esptool>=5.0.2 (modules/hal/espressif/zephyr/requirements.txt) and leaves
# bitstring unbounded, so it installs bitstring 5.0.0, which espefuse cannot
# import. The fix printed here installs both at the pinned versions.
#
# bitstring is NOT checked here. The build never imports it, and the eFuse
# probe before a flash runs whichever espefuse.py lib_efuse.sh found, which
# need not be this venv's. A broken one is reported there, by name, as a tool
# that does not start (see efuse_probe).
blink_zephyr_build_ready() {
	_bz_ws=$(blink_zephyr_ws) || {
		echo "FATAL: no Zephyr workspace found; see blink_zephyr_activate." >&2
		return 1
	}
	command -v esptool >/dev/null 2>&1 && return 0
	echo "FATAL: no esptool on PATH; an ESP32 firmware build cannot run without it." >&2
	if _bz_hint=$(blink_zephyr_pip_hint "$_bz_ws/.venv/bin/python"); then
		echo "       Install the versions this repo pins into the workspace venv:" >&2
		echo "         $_bz_hint" >&2
	else
		echo "       Install esptool and bitstring into $_bz_ws/.venv at the" >&2
		echo "       versions pc/requirements.txt pins (that file was not found from here)." >&2
	fi
	return 1
}
