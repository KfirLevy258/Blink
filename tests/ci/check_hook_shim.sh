#!/bin/sh
# Exercise tools/clauge-hook.sh under one specific shell.
#
#   tests/ci/check_hook_shim.sh [dash|busybox|bash|sh]
#
# Same reasoning as check_shim.sh, and the same stakes. This runs on EVERY tool
# call -- many times a minute during real work -- under whatever /bin/sh the
# customer's machine provides. A bashism here does not fail loudly: the hook
# exits non-zero or prints to the terminal, and a hook that misbehaves is a
# hook the user turns off.
#
# The one thing this shim must never do is emit anything. Claude Code runs it
# inline, so a stray byte on stdout or stderr lands in the user's session.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

WHICH="${1:-sh}"
ci_label "$WHICH"
SHIM_SRC="$ROOT/tools/clauge-hook.sh"
WORK="${TMPDIR:-/tmp}/clauge-hook-$WHICH"

case "$WHICH" in
dash) SH="dash" ;;
busybox) SH="busybox sh" ;;
bash) SH="bash" ;;
sh) SH="sh" ;;
*) echo "unknown shell: $WHICH" >&2; exit 1 ;;
esac
command -v "${SH%% *}" >/dev/null 2>&1 || { echo "$SH not installed" >&2; exit 1; }

rm -rf "$WORK"
HOME="$WORK/home"
mkdir -p "$HOME"
export HOME

SHIM="$HOME/clauge-hook.sh"
cp "$SHIM_SRC" "$SHIM"
# A realistic hook payload, including a field we must be seen not to keep.
PAYLOAD='{"session_id":"abc-123","transcript_path":"/x/y.jsonl","tool_name":"Bash","hook_event_name":"PreToolUse"}'

printf '== hook shim under %s\n' "$SH"

# 1. Records the event, prints nothing, says nothing.
out=$(printf '%s' "$PAYLOAD" | $SH "$SHIM" PreToolUse 2>"$WORK/err.txt")
[ -z "$out" ] || fail "printed to stdout: [$out]"
[ -s "$WORK/err.txt" ] && fail "wrote to stderr: $(cat "$WORK/err.txt")"
grep -q '"event":"PreToolUse"' "$HOME/.clauge/state.json" ||
	fail "event not recorded: $(cat "$HOME/.clauge/state.json" 2>/dev/null)"
ok "records the event, prints nothing, says nothing"

# 2. A timestamp that is actually a number. The state provider divides by
#    nothing and compares against now(); a quoted or empty value would parse
#    as malformed and the activity pip would never light.
grep -qE '"t":[0-9]{10}' "$HOME/.clauge/state.json" ||
	fail "timestamp is not a bare 10-digit epoch: $(cat "$HOME/.clauge/state.json")"
ok "timestamp is a bare epoch integer"

# 3. NOTHING from the payload is kept. This is the metadata-only promise, and
#    it is structural here: the shim never parses the payload at all.
for secret in abc-123 transcript_path Bash; do
	if grep -q "$secret" "$HOME/.clauge/state.json"; then
		fail "payload content leaked into state.json: $secret"
	fi
done
ok "keeps nothing from the payload"

# 4. Atomic write: a stray .tmp means the rename did not happen, and the daemon
#    can read a half-written file.
[ ! -e "$HOME/.clauge/state.json.tmp" ] || fail "left a .tmp file behind"
ok "atomic write leaves no temp file"

# 5. Exit status is always 0. A non-zero exit is a signal to Claude Code, and
#    Clauge having a bad day must not become the user's bad day.
printf '%s' "$PAYLOAD" | $SH "$SHIM" Stop >/dev/null 2>&1
[ $? -eq 0 ] || fail "non-zero exit on a normal run"
ok "exits 0"

# 6. Later events overwrite earlier ones -- one slot, newest wins.
printf '%s' "$PAYLOAD" | $SH "$SHIM" Stop >/dev/null 2>&1
grep -q '"event":"Stop"' "$HOME/.clauge/state.json" || fail "did not overwrite"
grep -q 'PreToolUse' "$HOME/.clauge/state.json" && fail "kept the old event"
ok "newest event wins"

# 7. No argument at all. Claude Code should always pass one, but a settings
#    file edited by hand may not, and the shim must not blow up over it.
out=$(printf '%s' "$PAYLOAD" | $SH "$SHIM" 2>"$WORK/err7.txt")
[ -z "$out" ] || fail "printed something with no argument: [$out]"
[ -s "$WORK/err7.txt" ] && fail "no-argument run wrote to stderr"
grep -q '"event":"unknown"' "$HOME/.clauge/state.json" ||
	fail "no-argument run did not record 'unknown'"
ok "a missing event name records 'unknown' rather than failing"

# 8. An unwritable HOME breaks the capture silently. Same rule as the
#    statusline shim: our own capture is allowed to be broken, but it must
#    never print on a path that runs many times a minute.
RO="$WORK/readonly"
mkdir -p "$RO"
chmod 500 "$RO"
out=$(HOME="$RO" sh -c "printf '%s' '$PAYLOAD' | $SH '$SHIM' PreToolUse" 2>"$WORK/err8.txt" || true)
[ -z "$out" ] || fail "printed with an unwritable HOME: [$out]"
[ -s "$WORK/err8.txt" ] && fail "unwritable HOME wrote to stderr: $(cat "$WORK/err8.txt")"
chmod 700 "$RO"
ok "an unwritable HOME breaks capture silently"

# 9. Stdin is drained. A hook that exits without reading gives its writer a
#    SIGPIPE; this checks a large payload is consumed without complaint.
big=$(awk 'BEGIN{printf "{\"pad\":\""; for(i=0;i<20000;i++) printf "x"; printf "\"}"}')
out=$(printf '%s' "$big" | $SH "$SHIM" PostToolUse 2>"$WORK/err9.txt")
[ -z "$out" ] || fail "printed on a large payload"
[ -s "$WORK/err9.txt" ] && fail "large payload wrote to stderr: $(cat "$WORK/err9.txt")"
ok "drains a large payload without complaint"

printf 'PASS [%s]\n' "$WHICH"
