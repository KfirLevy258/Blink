# Shared setup for the check_*.sh scripts in this directory. Sourced, not run.
#
#     . "$(dirname -- "$0")/lib.sh"
#     ci_label "install/$SCENARIO"    # optional, tags every fail/ok line
#     ci_binary                       # only if the script runs the binary
#
# Five scripts had five copies of the same four lines, and they had already
# drifted: each fail() carried a hand-written label, and the "did you build the
# binary yet" check was in three of them rather than all three that need it.
#
# ROOT is derived from $0, which in a sourced file is the SOURCING script --
# every caller lives in this directory, so ../.. is the repo root.
#
# Deliberately not in here: the throwaway HOME. Every script wants a slightly
# different one -- check_install.sh has a scenario whose whole point is a path
# with spaces in it -- and a helper that has to be argued with is worse than
# four lines of setup that say what they do.

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)

# Tag for fail/ok output, so a failure in a matrix job says which cell it was.
CI_LABEL=""
ci_label() { CI_LABEL="$1"; }

fail() {
	if [ -n "$CI_LABEL" ]; then
		printf 'FAIL [%s] %s\n' "$CI_LABEL" "$*" >&2
	else
		printf 'FAIL %s\n' "$*" >&2
	fi
	exit 1
}

ok() { printf '  ok   %s\n' "$*"; }

# The binary under test. CI builds it once per platform and passes the path in.
ci_binary() {
	BIN="${CLAUGE_BIN:-$ROOT/dist/clauge}"
	[ -x "$BIN" ] || BIN="$BIN.exe"
	[ -x "$BIN" ] || {
		echo "no binary at ${CLAUGE_BIN:-$ROOT/dist/clauge} -- run tools/build_binary.sh" >&2
		exit 1
	}
}
