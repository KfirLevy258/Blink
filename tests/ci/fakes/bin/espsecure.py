#!/bin/sh
# Stand-in for espsecure.py encrypt_flash_data: "encrypts" by copying, and
# records the address so the test can check each image went to its own.
echo "espsecure $*" >>"${FAKE_TOOL_LOG:?}"
out=""; in=""
while [ $# -gt 0 ]; do
	case "$1" in
	-o) out="$2"; shift 2 ;;
	*) in="$1"; shift ;;
	esac
done
cp "$in" "$out"
