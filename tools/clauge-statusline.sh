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
# file would parse as malformed and blank the panel. Failures here (disk full,
# unwritable HOME, ...) degrade silently -- Clauge's own capture is allowed to
# be broken, but that must never print to the terminal on every render.
mkdir -p "$HOME/.clauge" 2>/dev/null
# 2>/dev/null must come BEFORE the '>' target on this line, not after: if
# opening the target itself fails (e.g. the mkdir above also failed), the
# shell reports that failure using whatever stderr was in effect when the '>'
# was processed. A trailing '2>/dev/null' hasn't been applied yet at that
# point, so it looks like it suppresses the error but doesn't -- confirmed
# leaking "Permission denied" on every render under both sh and dash before
# this was reordered.
printf '%s' "$input" 2>/dev/null > "$HOME/.clauge/statusline.json.tmp" &&
  mv -f "$HOME/.clauge/statusline.json.tmp" "$HOME/.clauge/statusline.json" 2>/dev/null

# Delegate to the previously configured command, if any. Never fail the status
# bar because Clauge had a problem.
#
# Deliberately no timeout on the chain call below. A hang here is not a
# regression: this same command ran directly as the user's statusline before
# Clauge existed, so it hung identically then, and whatever timeout Claude
# Code applies to a statusline command still bounds this whole script from
# the outside. POSIX sh has no portable timeout(1) (absent on stock macOS),
# and a background-plus-kill substitute would fork two extra processes on
# every render -- many times a minute -- to guard a failure mode the user
# already had.
#
# The payload write above MUST stay above this line. Capture happens before
# delegation on purpose, so a wedged chain command cannot starve the panel of
# fresh data: Clauge keeps getting current numbers even while the user's own
# statusline is hung. Do not reorder this to "only record on success" -- that
# would let a hanging chain block Clauge's own capture too, which is exactly
# the failure this ordering exists to prevent.
CHAIN="$HOME/.clauge/statusline-chain"
if [ -s "$CHAIN" ]; then
  chain_cmd=$(cat "$CHAIN")
  # Refuse to chain into ourselves. The installer has one ambiguous case (its
  # own marker lost, shim path changed since the last install) where it
  # deliberately records our own old shim as "previous" rather than silently
  # discarding what might be a real customer command -- so the chain file can
  # legitimately contain a Clauge shim invocation. Without this guard that
  # would either loop or, once the old shim hits this same check against its
  # own $0, execute one harmless extra hop; with it, the very first hop stops.
  #
  # This is a literal string comparison, not a resolved-path comparison: the
  # installer always writes this line as exactly "sh <shim_path>", and POSIX
  # guarantees a script run as `sh <path>` sees $0 set to that path operand
  # verbatim, uncanonicalised. Both sides trace back to the same install-time
  # string, so byte equality answers the question that matters -- "would this
  # line re-run this exact invocation?" -- without needing realpath/readlink
  # -f, neither of which stock macOS ships reliably.
  if [ "$chain_cmd" != "sh $0" ]; then
    printf '%s' "$input" | sh -c "$chain_cmd"
  fi
fi
exit 0
