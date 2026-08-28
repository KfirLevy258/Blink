#!/bin/sh
# Exercise tools/blink-statusline.sh under one specific shell.
#
#   tests/ci/check_shim.sh [dash|busybox|bash|sh]
#
# This runs on every status line render, under whatever /bin/sh the customer's
# machine provides -- dash on Debian and Ubuntu, busybox on a slim container,
# bash on many Macs. A bashism here does not fail loudly; it silently stops the
# capture and the panel goes stale looking exactly like "Claude Code is not
# running", which is the failure this whole design is built to avoid.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

WHICH="${1:-sh}"
ci_label "$WHICH"
SHIM_SRC="$ROOT/tools/blink-statusline.sh"
WORK="${TMPDIR:-/tmp}/blink-shim-$WHICH"

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

SHIM="$HOME/blink-statusline.sh"
cp "$SHIM_SRC" "$SHIM"
PAYLOAD='{"rate_limits":{"five_hour":{"used_percentage":7,"resets_at":11}}}'
CAP="$HOME/.blink/statusline.json"


printf '== shim under %s\n' "$SH"

# 1. Capture with no chain configured.
out=$(printf '%s' "$PAYLOAD" | $SH "$SHIM" 2>"$WORK/err.txt")
[ -z "$out" ] || fail "printed something with no chain: [$out]"
[ -s "$WORK/err.txt" ] && fail "wrote to stderr: $(cat "$WORK/err.txt")"
[ "$(cat "$CAP")" = "$PAYLOAD" ] || fail "payload not captured verbatim"
ok "captures the payload, prints nothing, says nothing"

# 2. No temp file left behind -- the daemon globs nothing, but a stray
#    statusline.json.tmp means the atomic rename did not happen.
#    The name carries the writer's pid, so this is a glob, not one path.
for t in "$HOME/.blink"/statusline.json.*tmp; do
	[ ! -e "$t" ] || fail "left a temp file behind: $t"
done
ok "atomic write leaves no temp file"

# 3. Chain: their command runs, gets the SAME input, and its output passes
#    through untouched. This is the promise that their bar still renders.
cat >"$HOME/their-bar.sh" <<EOF
#!/bin/sh
cat >"$HOME/what-they-got"
printf 'BAR-OUTPUT'
EOF
chmod 755 "$HOME/their-bar.sh"
echo "sh $HOME/their-bar.sh" >"$HOME/.blink/statusline-chain"

out=$(printf '%s' "$PAYLOAD" | $SH "$SHIM" 2>"$WORK/err2.txt")
[ "$out" = "BAR-OUTPUT" ] || fail "chained output was [$out]"
[ "$(cat "$HOME/what-they-got")" = "$PAYLOAD" ] ||
	fail "the chained command did not receive the same input"
[ -s "$WORK/err2.txt" ] && fail "chaining wrote to stderr: $(cat "$WORK/err2.txt")"
ok "chained command runs, sees the same input, output passes through"

# 4. A chain pointing at something that no longer exists must stay silent.
#    Not cosmetic: this prints on EVERY render, into the middle of a prompt.
echo "sh $HOME/deleted-bar.sh" >"$HOME/.blink/statusline-chain"
printf '%s' "$PAYLOAD" | $SH "$SHIM" >/dev/null 2>"$WORK/err3.txt"
[ -s "$WORK/err3.txt" ] && fail "broken chain leaked: $(cat "$WORK/err3.txt")"
ok "a broken chain command prints nothing"

# 5. Self-reference must not recurse. The installer has one ambiguous case
#    where it records our own shim as "previous"; a shim that chained into
#    itself would fork forever on every render.
echo "sh $SHIM" >"$HOME/.blink/statusline-chain"
printf '%s' "$PAYLOAD" | $SH "$SHIM" >/dev/null 2>"$WORK/err4.txt"
[ -s "$WORK/err4.txt" ] && fail "self-chain leaked: $(cat "$WORK/err4.txt")"
ok "refuses to chain into itself"

# 5b. ...including when the chain records the WINDOWS form. The installer
#     writes "bash <path>" there, not "sh <path>", because Claude Code rewrites
#     a status line command mentioning a .sh file otherwise. The guard compared
#     the whole command line, so on Windows it could never match its own entry
#     and the bounded one-extra-hop became unbounded.
#
#     Bounded here on purpose: a regression is an infinite fork, so the test
#     has to be the thing that stops, not the shim.
printf 'bash %s\n' "$SHIM" >"$HOME/.blink/statusline-chain"
printf '%s' "$PAYLOAD" | $SH "$SHIM" >/dev/null 2>&1 &
shim_pid=$!
sleep 3
if kill -0 "$shim_pid" 2>/dev/null; then
	kill -9 "$shim_pid" 2>/dev/null
	fail "the bash-form self-reference recursed (alive after 3 s)"
fi
wait "$shim_pid" 2>/dev/null || true
ok "refuses to chain into itself when the chain says bash, not sh"

# 6. Same, with a path containing spaces -- the shim reconstructs shlex.quote's
#    quoting in shell to recognise itself, and the two must agree byte for byte
#    or the guard fails open exactly when the path is quoted.
SPACED="$HOME/a dir with spaces"
mkdir -p "$SPACED"
cp "$SHIM_SRC" "$SPACED/blink-statusline.sh"
printf "sh '%s/blink-statusline.sh'\n" "$SPACED" >"$HOME/.blink/statusline-chain"
printf '%s' "$PAYLOAD" | $SH "$SPACED/blink-statusline.sh" >/dev/null 2>"$WORK/err5.txt"
[ -s "$WORK/err5.txt" ] && fail "spaced self-chain leaked: $(cat "$WORK/err5.txt")"
ok "recognises itself at a path containing spaces"

# 7. An unwritable HOME degrades silently. Blink's own capture is allowed to
#    break; it is never allowed to print into someone's terminal.
UNWRITABLE="$WORK/readonly"
mkdir -p "$UNWRITABLE"
chmod 500 "$UNWRITABLE"
rm -f "$WORK/err6.txt"
HOME="$UNWRITABLE" $SH "$SHIM" </dev/null >/dev/null 2>"$WORK/err6.txt" || true
chmod 700 "$UNWRITABLE"
[ -s "$WORK/err6.txt" ] && fail "unwritable HOME leaked: $(cat "$WORK/err6.txt")"
ok "an unwritable HOME breaks capture silently"


printf 'PASS [%s]\n' "$WHICH"
