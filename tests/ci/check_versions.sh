#!/bin/sh
# The firmware and the daemon ship from one tag, so they must agree about what
# that tag is -- and about the protocol they speak over the cable.
#
# Nothing else can catch a mismatch. A daemon claiming 0.6.0 while the board it
# flashes reports 0.5.1 is not a build failure, not a test failure, and not
# visible on screen; it is a support call six weeks later from someone whose
# install is half-updated. This is two greps, run in CI and again as the first
# thing tools/release.sh does.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

VH="$ROOT/firmware/src/version.h"
PV="$ROOT/pc/version.py"

fw=$(sed -n 's/^#define CLAUGE_FW_VERSION "\(.*\)"$/\1/p' "$VH")
fw_proto=$(sed -n 's/^#define CLAUGE_PROTO_VERSION \([0-9][0-9]*\).*$/\1/p' "$VH")
pc=$(sed -n 's/^RELEASE_VERSION = "\(.*\)"$/\1/p' "$PV")
pc_proto=$(sed -n 's/^PROTO_VERSION = \([0-9][0-9]*\)$/\1/p' "$PV")

fail=0
for pair in "fw:$fw" "fw_proto:$fw_proto" "pc:$pc" "pc_proto:$pc_proto"; do
	name=${pair%%:*}
	value=${pair#*:}
	if [ -z "$value" ]; then
		echo "FATAL: could not read $name -- has the declaration moved?" >&2
		fail=1
	fi
done
[ "$fail" -eq 0 ] || exit 1

if [ "$fw" != "$pc" ]; then
	echo "FATAL: release version disagrees." >&2
	echo "       firmware/src/version.h says $fw" >&2
	echo "       pc/version.py says          $pc" >&2
	echo "       They ship from one tag; bump both." >&2
	fail=1
fi
if [ "$fw_proto" != "$pc_proto" ]; then
	echo "FATAL: protocol version disagrees ($fw_proto vs $pc_proto)." >&2
	echo "       Both sides must move together, and only for a change that" >&2
	echo "       genuinely breaks an older peer -- additive fields do not." >&2
	fail=1
fi
[ "$fail" -eq 0 ] || exit 1

echo "versions agree: release $fw, protocol $fw_proto"
