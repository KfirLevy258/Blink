#!/bin/sh
# Program one CLAUDE unit. See tools/burn.sh.
exec "$(dirname -- "$0")/burn.sh" --edition claude "$@"
