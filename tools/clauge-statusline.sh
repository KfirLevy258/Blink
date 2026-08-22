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
# Guarded, because this runs on EVERY status line render -- many times a
# minute -- and an unconditional mkdir forks a process each time to create a
# directory that has existed since the first one.
[ -d "$HOME/.clauge" ] || mkdir -p "$HOME/.clauge" 2>/dev/null
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
  # `read`, not `cat`: a builtin instead of a fork, on the same every-render
  # path. The chain file is one line by construction (install writes exactly
  # one), and IFS= plus -r keeps it byte-for-byte -- leading or trailing
  # whitespace and backslashes intact, which a command string may contain.
  IFS= read -r chain_cmd < "$CHAIN" || chain_cmd=""
  # Refuse to chain into ourselves. The installer has one ambiguous case (its
  # own marker lost, shim path changed since the last install) where it
  # deliberately records our own old shim as "previous" rather than silently
  # discarding what might be a real customer command -- so the chain file can
  # legitimately contain a Clauge shim invocation. In that specific case the
  # path HAS changed (that's why it's ambiguous), so this guard does not stop
  # things on the first hop: the shim now running (at the new path) sees
  # chain_cmd naming the OLD path, the two differ, and it calls through to
  # the old shim once. That old shim then hits this same check against its
  # own $0, which now DOES match chain_cmd, and stops there. One extra hop,
  # bounded, and harmless -- not zero hops.
  #
  # This is a literal string comparison, not a resolved-path comparison: the
  # installer always writes this line as exactly "sh <shim_path>", quoted
  # exactly the way Python's shlex.quote would quote it (bare when the path
  # needs no quoting, single-quoted with '"'"'-escaping when it does -- e.g.
  # a path containing a space). POSIX guarantees a script run as `sh <path>`
  # sees $0 set to that path operand verbatim, uncanonicalised and WITHOUT
  # any quote characters (those were consumed by the shell that invoked us).
  # So this must reconstruct the quoted form here, mirroring shlex.quote's
  # rule byte for byte, rather than comparing "sh $0" directly -- otherwise
  # this guard would falsely treat a legitimate self-reference as foreign the
  # moment shim_path contains a space, chaining forever.
  self=$0
  case $self in
    *[!A-Za-z0-9@%+=:,./_-]*)
      escaped=$(printf '%s' "$self" | sed "s/'/'\"'\"'/g")
      self="'$escaped'"
      ;;
  esac
  # Compare the PATH OPERAND, not the whole command line. The installer writes
  # "sh <path>" on POSIX and "bash <path>" on Windows -- see
  # statusline_command(), which has to start with "bash " there or Claude Code
  # rewrites the line. Matching the literal string "sh $self" therefore could
  # never match on Windows, so the guard was absent on exactly the platform it
  # was written for, and the bounded one extra hop below became unbounded.
  chain_arg=${chain_cmd#sh }
  chain_arg=${chain_arg#bash }
  if [ "$chain_arg" != "$self" ]; then
    # 2>/dev/null: the installer's ambiguous case above can point this at a
    # shim path that no longer exists (superseded by a later install at a
    # different path), which makes `sh -c` print "No such file or
    # directory" to stderr on every single render -- exactly the terminal
    # leak an earlier commit removed from the write path. This line
    # deliberately drops ALL of the chained command's stderr, not just that
    # one message: Clauge must never contribute terminal noise regardless of
    # source, and POSIX sh has no way to filter for one specific error text.
    printf '%s' "$input" | sh -c "$chain_cmd" 2>/dev/null
  fi
fi
exit 0
