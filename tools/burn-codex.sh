#!/bin/sh
# Program one CODEX unit. See tools/burn.sh.
exec "$(dirname -- "$0")/burn.sh" --edition codex "$@"
