#!/bin/sh
# Clauge statusline shim.
#
# Claude Code pipes its statusline JSON to this script on every render. We keep
# a copy for the Clauge daemon, then hand the SAME input to whatever statusline
# command was configured before Clauge was installed, so the user's status bar
# is unchanged.
#
# Nothing here reads a credential: the payload contains only the two usage
# percentages Claude Code has already computed.
input=$(cat)

# Atomic write: the daemon may read this file at any moment, and a half-written
# file would parse as malformed and blank the panel.
mkdir -p "$HOME/.clauge"
printf '%s' "$input" > "$HOME/.clauge/statusline.json.tmp" 2>/dev/null &&
  mv -f "$HOME/.clauge/statusline.json.tmp" "$HOME/.clauge/statusline.json" 2>/dev/null

# Delegate to the previously configured command, if any. Never fail the status
# bar because Clauge had a problem.
CHAIN="$HOME/.clauge/statusline-chain"
if [ -s "$CHAIN" ]; then
  printf '%s' "$input" | sh -c "$(cat "$CHAIN")"
fi
exit 0
