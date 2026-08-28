#!/bin/sh
# Watch the Codex rollout-log contract where it is defined.
#
#   tests/ci/check_codex_contract.sh [git-ref]      default: main
#
# pc/providers/codex_cli.py reads `rate_limits` out of Codex's token_count
# event: primary/secondary windows, each with used_percent, window_minutes and
# resets_at. Codex is open source, and those are Rust struct fields in
# codex-rs/protocol/src/protocol.rs, serialised under their own names. So the
# contract can be checked at the ref that ships, without an account.
#
# WHAT THIS CANNOT DO: produce a rollout. Codex writes one only for a signed-in
# session, and an API key would not carry the ChatGPT-plan windows anyway. The
# parser itself is pinned to a real captured log in tests/fixtures.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

REF="${1:-main}"
ci_label "$REF"
SRC="https://raw.githubusercontent.com/openai/codex/$REF/codex-rs/protocol/src/protocol.rs"
WORK="${TMPDIR:-/tmp}/blink-codex-contract"
mkdir -p "$WORK"
FILE="$WORK/protocol.rs"

printf '== Codex protocol at %s\n' "$REF"
curl -fsSL "$SRC" -o "$FILE" || fail "could not fetch $SRC"
[ -s "$FILE" ] || fail "empty protocol.rs"

# 1. The event carries the snapshot.
grep -q 'pub rate_limits: Option<RateLimitSnapshot>' "$FILE" ||
	fail "token_count no longer carries rate_limits: Option<RateLimitSnapshot>"
ok "the event still carries rate_limits"

# 2. The snapshot has the two windows, under the names we read.
grep -q 'pub struct RateLimitSnapshot' "$FILE" || fail "RateLimitSnapshot is gone"
grep -q 'pub primary: Option<RateLimitWindow>' "$FILE" || fail "primary window renamed or gone"
grep -q 'pub secondary: Option<RateLimitWindow>' "$FILE" || fail "secondary window renamed or gone"
ok "primary and secondary windows are still there"

# 3. Each window has the three fields, under the names we read.
grep -q 'pub struct RateLimitWindow' "$FILE" || fail "RateLimitWindow is gone"
for field in 'pub used_percent: f64' 'pub window_minutes: Option<i64>' 'pub resets_at: Option<i64>'; do
	grep -q "$field" "$FILE" || fail "RateLimitWindow lost '$field'"
done
ok "used_percent, window_minutes and resets_at are still there"

# 4. And none of them is serialised under another name. A #[serde(rename)]
#    on the field, or rename_all on the struct, changes the JSON without
#    touching the Rust name the greps above look for.
for st in RateLimitSnapshot RateLimitWindow; do
	if grep -B4 "pub struct $st" "$FILE" | grep -q 'rename_all'; then
		fail "$st is serialised with rename_all -- the JSON keys have moved"
	fi
done
for field in 'pub rate_limits:' 'pub primary:' 'pub secondary:' 'pub used_percent:' 'pub window_minutes:' 'pub resets_at: Option'; do
	if grep -B2 "$field" "$FILE" | grep -q 'serde(rename\b\|serde(rename ='; then
		fail "'$field' is serialised under another name"
	fi
done
ok "no serde renames on the fields we read"

printf 'PASS [codex contract at %s]\n' "$REF"
