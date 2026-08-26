#!/bin/sh
# Clauge execution-state hook.
#
# Claude Code runs this on lifecycle events and pipes the event JSON to stdin.
# We record ONE thing: which event fired, and when. Nothing from the payload is
# read or kept -- not the prompt, not the tool arguments, not the transcript
# path, not even the session id. The execution state on the panel is derived
# entirely from an event name and a clock, which is the strongest form of the
# metadata-only promise this product makes: there is nothing here to leak
# because nothing is captured.
#
# The event name arrives as $1, from the settings entry we wrote, rather than
# being parsed out of the payload. POSIX sh has no JSON parser, and adding one
# on a path that runs on every tool call would be a fork per call to learn
# something the caller already knows.
event=${1:-unknown}

# Drain stdin. Claude Code writes the payload whether we want it or not, and a
# hook that exits without reading gives the writer a SIGPIPE.
cat > /dev/null 2>&1

[ -d "$HOME/.clauge" ] || mkdir -p "$HOME/.clauge" 2>/dev/null

# Atomic write, same reason as the statusline shim: the daemon may read at any
# moment and a half-written file parses as malformed. The 2>/dev/null must come
# BEFORE the '>' target -- if opening the target itself fails, the shell reports
# it using whatever stderr was in effect when the '>' was processed, so a
# trailing redirect looks like it suppresses the error and does not.
#
# `date +%s` is the one fork here, and it is unavoidable: POSIX sh has no clock.
printf '{"event":"%s","t":%s}' "$event" "$(date +%s)" 2>/dev/null \
  > "$HOME/.clauge/state.json.tmp" &&
  mv -f "$HOME/.clauge/state.json.tmp" "$HOME/.clauge/state.json" 2>/dev/null

# Never fail a hook. A non-zero exit from a hook is a signal to Claude Code,
# and Clauge having a bad day must not become the user's bad day.
exit 0
