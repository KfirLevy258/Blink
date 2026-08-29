#!/bin/sh
# Stand-in for esptool.py (tests/ci/check_factory.sh). Records every call to
# $FAKE_TOOL_LOG and prints what the real tool prints for the lines the burn
# scripts grep for. FAKE_ESPTOOL_FAIL=<op> makes that one operation exit 1.
echo "esptool $*" >>"${FAKE_TOOL_LOG:?}"
op=""
for a in "$@"; do
	case "$a" in write_flash|erase_region|run) op="$a" ;; esac
done
if [ "${FAKE_ESPTOOL_FAIL:-}" = "$op" ]; then
	echo "A fatal error occurred: simulated $op failure"
	exit 1
fi
case "$op" in
write_flash)
	while [ $# -gt 0 ] && [ "$1" != write_flash ]; do shift; done
	shift
	while [ $# -ge 2 ]; do
		echo "Wrote $(wc -c <"$2" | tr -d ' ') bytes at $1 in 0.1 seconds"
		echo "Hash of data verified."
		shift 2
	done
	;;
erase_region) echo "Erase completed successfully in 0.1 seconds." ;;
esac
echo "Hard resetting via RTS pin..."
exit 0
