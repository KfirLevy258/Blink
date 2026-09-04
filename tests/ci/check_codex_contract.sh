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
# It also checks WHERE the log is written, which the reader assumes just as
# hard as the field names: $CODEX_HOME or ~/.codex, then sessions/YYYY/MM/DD,
# then rollout-<stamp>-<id>.jsonl -- and that Codex only compresses a rollout
# to .jsonl.zst after it is days old, so the freshest reading is always in a
# plain file the reader's *.jsonl glob can see.
#
# WHAT THIS CANNOT DO: produce a rollout. Codex writes one only for a signed-in
# session, and an API key would not carry the ChatGPT-plan windows anyway. The
# parser itself is pinned to a real captured log in tests/fixtures.
#
# CODEX_SRC_DIR=<dir> reads the files from a directory instead of fetching
# them, for running this where curl is not available.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

REF="${1:-main}"
ci_label "$REF"
RAW="https://raw.githubusercontent.com/openai/codex/$REF"
WORK="${TMPDIR:-/tmp}/blink-codex-contract"
mkdir -p "$WORK"

# fetch <repo path> -> prints the local file holding it.
fetch() {
	name=$(basename "$1")
	case "$1" in
	*home-dir*) name="home-dir-$name" ;;
	*rollout/src*) name="rollout-$name" ;;
	esac
	if [ -n "${CODEX_SRC_DIR:-}" ]; then
		[ -s "$CODEX_SRC_DIR/$name" ] || fail "no $name under CODEX_SRC_DIR"
		printf '%s' "$CODEX_SRC_DIR/$name"
		return
	fi
	curl -fsSL "$RAW/$1" -o "$WORK/$name" || fail "could not fetch $RAW/$1"
	[ -s "$WORK/$name" ] || fail "empty $name"
	printf '%s' "$WORK/$name"
}

printf '== Codex protocol at %s\n' "$REF"
FILE=$(fetch codex-rs/protocol/src/protocol.rs)

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

# 5. Where the log lives. pc/providers/codex_cli.py: $CODEX_HOME, else
#    ~/.codex; then sessions/<year>/<month>/<day>/rollout-*.jsonl.
HOMEDIR=$(fetch codex-rs/utils/home-dir/src/lib.rs)
grep -q 'std::env::var("CODEX_HOME")' "$HOMEDIR" || fail "CODEX_HOME is no longer honoured"
grep -q 'p.push(".codex")' "$HOMEDIR" || fail "the default home is no longer ~/.codex"
ok "home is \$CODEX_HOME, else ~/.codex"

ROLLOUT_LIB=$(fetch codex-rs/rollout/src/lib.rs)
grep -q 'SESSIONS_SUBDIR: &str = "sessions"' "$ROLLOUT_LIB" || fail "the sessions subdirectory is no longer 'sessions'"
RECORDER=$(fetch codex-rs/rollout/src/recorder.rs)
grep -q 'dir.push(SESSIONS_SUBDIR)' "$RECORDER" || fail "rollouts no longer go under the sessions subdirectory"
grep -q 'dir.push(timestamp.year().to_string())' "$RECORDER" || fail "the year directory is gone"
grep -q 'u8::from(timestamp.month())' "$RECORDER" || fail "the month directory is gone"
grep -q 'timestamp.day()' "$RECORDER" || fail "the day directory is gone"
ok "logs go under sessions/YYYY/MM/DD"

COMPRESSION=$(fetch codex-rs/rollout/src/compression.rs)
grep -q 'name.starts_with("rollout-") && name.ends_with(".jsonl")' "$COMPRESSION" ||
	fail "a rollout is no longer named rollout-*.jsonl"
ok "files are named rollout-*.jsonl"

# 6. Compression. Rollouts older than MIN_ROLLOUT_AGE become .jsonl.zst,
#    which the reader's glob does not see. That is fine exactly as long as
#    the age is days, not minutes: the freshest reading is then always in a
#    plain file. The reader would need zstd the day this drops under a day.
grep -q 'COMPRESSED_SUFFIX: &str = ".zst"' "$COMPRESSION" || fail "the compressed suffix changed"
age=$(sed -n 's/^[[:space:]]*const MIN_ROLLOUT_AGE: Duration = Duration::from_secs(\([0-9]*\) \* 24 \* 60 \* 60);$/\1/p' "$COMPRESSION")
[ -n "$age" ] || fail "MIN_ROLLOUT_AGE is no longer expressed in days; read compression.rs"
[ "$age" -ge 1 ] || fail "rollouts are compressed after $age days -- the reader must learn .zst"
ok "rollouts are only compressed after $age days, so the freshest is always plain .jsonl"

# 7. The turn events pc/providers/codex_cli.parse_rollout_state keys on.
#    The aliases are upstream telling us a rename is coming: the v2 wire
#    spells these turn_started/turn_complete. The day the alias becomes the
#    primary name the state machine goes silent and every Python test stays
#    green, because they all feed the reader strings this repo wrote.
grep -q '#\[serde(rename = "task_started", alias = "turn_started")\]' "$FILE" ||
	fail "task_started is no longer the serialised name -- see the turn_started alias"
grep -q '#\[serde(rename = "task_complete", alias = "turn_complete")\]' "$FILE" ||
	fail "task_complete is no longer the serialised name -- see the turn_complete alias"
ok "task_started and task_complete are still the wire names"

# 8. Failure. The reader calls a turn failed when its task_complete carries
#    an `error` object, and UsageLimitExceeded is the case it exists for.
grep -q 'pub struct TurnCompleteEvent' "$FILE" || fail "TurnCompleteEvent is gone"
grep -q 'pub error: Option<ErrorEvent>' "$FILE" ||
	fail "TurnCompleteEvent no longer carries error: Option<ErrorEvent>"
grep -q 'pub codex_error_info: Option<CodexErrorInfo>' "$FILE" ||
	fail "ErrorEvent no longer carries codex_error_info"
grep -q 'UsageLimitExceeded,' "$FILE" ||
	fail "CodexErrorInfo lost UsageLimitExceeded -- the case this reader exists for"
ok "task_complete still reports failure through error/codex_error_info"

# 9. The two errors that do NOT fail a turn. codex_cli._NOT_A_TURN_FAILURE
#    mirrors this exact arm; a variant leaving it would have the panel go red
#    for something upstream does not call a failure, and a variant joining it
#    would have us keep painting red for something that stopped being one.
grep -A2 'pub fn affects_turn_status' "$FILE" |
	grep -q 'Self::ThreadRollbackFailed | Self::ActiveTurnNotSteerable { \.\. } => false,' ||
	fail "affects_turn_status' not-a-failure arm changed -- see codex_cli._NOT_A_TURN_FAILURE"
ok "thread_rollback_failed and active_turn_not_steerable are still not turn failures"

# 10. turn_aborted stays a user action. All four reasons are things the
#     person did, which is why the reader maps every one of them to idle
#     rather than to red.
grep -q 'pub enum TurnAbortReason' "$FILE" || fail "TurnAbortReason is gone"
for reason in Interrupted Replaced ReviewEnded BudgetLimited; do
	grep -A5 'pub enum TurnAbortReason' "$FILE" |
		grep -q "^[[:space:]]*$reason,$" ||
		fail "TurnAbortReason lost $reason -- re-read whether turn_aborted still means idle"
done
ok "turn_aborted still means the person stopped the turn"

# 11. The project name. Line 1 of a rollout is a session_meta record, and its
#     cwd is the only place a Codex session's name can come from -- the
#     filename carries a timestamp and a UUID and nothing else.
grep -q 'pub struct SessionMeta' "$FILE" || fail "SessionMeta is gone"
grep -A20 'pub struct SessionMeta' "$FILE" | grep -q 'pub cwd: PathBuf' ||
	fail "SessionMeta no longer carries cwd -- Codex sessions cannot be named"
ok "session_meta still carries cwd"

POLICY=$(fetch codex-rs/rollout/src/policy.rs)
grep -q 'RolloutItem::SessionMeta(_) => true' "$POLICY" ||
	fail "session_meta is no longer persisted unconditionally"
ok "session_meta is still written to every rollout"

printf 'PASS [codex contract at %s]\n' "$REF"
