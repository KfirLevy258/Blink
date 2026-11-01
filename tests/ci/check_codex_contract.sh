#!/bin/sh
# Watch the Codex rollout-log contract where it is defined.
#
#   tests/ci/check_codex_contract.sh [git-ref]      default: main
#
# pc/providers/codex_cli.py reads `rate_limits` out of Codex's token_count
# event: primary/secondary windows, each with used_percent, window_minutes and
# resets_at. pc/burn_reader.CodexBurnReader reads the same event's `info`:
# the token figures of total_token_usage and last_token_usage (check 12). Codex is open source, and those are Rust struct fields in
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
# shellcheck source=tests/ci/lib_rust.sh
. "$(dirname -- "$0")/lib_rust.sh"

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

# field_attrs FILE ITEM FIELD -> the attributes written on FIELD inside ITEM,
# on one line. FIELD is the start of the field's (or variant's) own line,
# `pub output_tokens:` or `TokenCount(`. Everything between the line that
# ended the field before it and the field itself is its block: attributes,
# doc comments and the continuation lines of an attribute split over
# several. Prints nothing for a field with none, or none found.
field_attrs() {
	rust_item "$1" "$2" | awk -v fld="$3" '
	{ line = $0; sub(/^[ \t]+/, "", line) }
	inattr {
		block = block " " line
		if (line ~ /\][ \t]*$/) inattr = 0
		next
	}
	index(line, fld) == 1 { print block; exit }
	line ~ /^#\[/ {
		block = block " " line
		if (line !~ /\][ \t]*$/) inattr = 1
		next
	}
	line ~ /^\/\// { next }
	{ block = "" }'
}

# serialised_elsewhere ATTRS -> true when a #[serde(...)] in ATTRS moves the
# field's JSON key or leaves it out: rename (rename = "..." and
# rename(serialize = ...) alike), skip, skip_serializing or
# skip_serializing_if, wherever in the attribute it is written. Upstream
# puts `default` first as a habit (TokenUsage.cache_write_input_tokens), and
# a check that only caught a rename written first missed
# `#[serde(default, rename = "completion_tokens")]` (3rd review,
# 2026-11-01). Only serde's own attribute: #[ts(skip)] and
# #[schemars(skip)] are about generated TypeScript and JSON Schema, not the
# JSON Codex writes. rename_all and alias do not match, and are not this.
serialised_elsewhere() {
	printf '%s\n' "$1" | grep -Eq \
		'serde[(]([^]]*[^A-Za-z0-9_])?(rename|skip|skip_serializing|skip_serializing_if)([^A-Za-z0-9_]|$)'
}

# check_fields FILE ITEM LABEL FIELD... -> fail on the first FIELD of ITEM
# that serialised_elsewhere says is not written under its own name.
check_fields() {
	cf_file=$1 cf_item=$2 cf_label=$3
	shift 3
	for cf_field in "$@"; do
		if serialised_elsewhere "$(field_attrs "$cf_file" "$cf_item" "$cf_field")"; then
			fail "$cf_label '$cf_field' is renamed or skipped by serde -- the JSON key has moved or gone"
		fi
	done
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
#    Read off each field's own attributes, inside its own struct
#    (field_attrs): a `grep -B2` for 'serde(rename' caught a rename only
#    when it was written first, and nothing caught skip (3rd review,
#    2026-11-01).
check_fields "$FILE" 'pub struct TokenCountEvent' TokenCountEvent 'pub rate_limits:'
check_fields "$FILE" 'pub struct RateLimitSnapshot' RateLimitSnapshot 'pub primary:' 'pub secondary:'
check_fields "$FILE" 'pub struct RateLimitWindow' RateLimitWindow 'pub used_percent:' 'pub window_minutes:' 'pub resets_at:'
ok "no serde renames or skips on the fields we read"

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
rust_item "$FILE" 'pub fn affects_turn_status' |
	grep -q 'Self::ThreadRollbackFailed | Self::ActiveTurnNotSteerable { \.\. } => false,' ||
	fail "affects_turn_status' not-a-failure arm changed -- see codex_cli._NOT_A_TURN_FAILURE"
ok "thread_rollback_failed and active_turn_not_steerable are still not turn failures"

# 10. turn_aborted stays a user action. All four reasons are things the
#     person did, which is why the reader maps every one of them to idle
#     rather than to red.
grep -q 'pub enum TurnAbortReason' "$FILE" || fail "TurnAbortReason is gone"
for reason in Interrupted Replaced ReviewEnded BudgetLimited; do
	rust_item "$FILE" 'pub enum TurnAbortReason' |
		grep -q "^[[:space:]]*$reason,$" ||
		fail "TurnAbortReason lost $reason -- re-read whether turn_aborted still means idle"
done
ok "turn_aborted still means the person stopped the turn"

# 11. The project name. Line 1 of a rollout is a session_meta record, and its
#     cwd is the only place a Codex session's name can come from -- the
#     filename carries a timestamp and a UUID and nothing else.
grep -q 'pub struct SessionMeta' "$FILE" || fail "SessionMeta is gone"
# The whole struct, however long it grows: a 20-line window reported cwd
# gone the day upstream pushed it to line 23 (tests/ci/lib_rust.sh).
rust_item "$FILE" 'pub struct SessionMeta' | grep -q 'pub cwd: PathBuf' ||
	fail "SessionMeta no longer carries cwd -- Codex sessions cannot be named"
ok "session_meta still carries cwd"

POLICY=$(fetch codex-rs/rollout/src/policy.rs)
# Two spellings of "always". Up to some release the policy was a bool,
# `=> true`; by rust-v0.163.0 it returns the item to persist, and an
# unconditional arm reads `=> Some(Cow::Borrowed(item))`. The bool form
# alone failed this check against a Codex that still wrote session_meta to
# every rollout (CI, 2026-10-19). A conditional arm would read
# `.then_some(...)` or `matches!(...)`, and matches neither.
grep -q -E 'RolloutItem::SessionMeta\(_\) => (true|Some\(Cow::Borrowed\(item\)\))' "$POLICY" ||
	fail "session_meta is no longer persisted unconditionally"
ok "session_meta is still written to every rollout"

# 12. The burn view's figures. pc/burn_reader.CodexBurnReader counts the
#     output_tokens of each token_count event's info.last_token_usage, and
#     recognises an event written twice by every figure of total_token_usage
#     and last_token_usage (CODEX_USAGE_FIELDS). Renamed, the reader would
#     find nothing to count and the page would draw twenty empty bars, a
#     confident "idle", with every Python test green -- they feed it events
#     this repo wrote (/code-review max, 2026-11-01).
rust_item "$FILE" 'pub enum EventMsg' | grep -q '^[[:space:]]*TokenCount(TokenCountEvent),' ||
	fail "EventMsg lost TokenCount(TokenCountEvent) -- token_count events are gone"
EVENT_ATTRS=$(grep -B4 'pub enum EventMsg' "$FILE" | grep 'serde(' || true)
printf '%s\n' "$EVENT_ATTRS" | grep -q 'rename_all = "snake_case"' ||
	fail "EventMsg is no longer snake_case -- the event is not called token_count"
# The reader matches payload.type == "token_count": EventMsg must still be
# internally tagged by `type`, with the variant's fields beside the tag. A
# tag of another name, an adjacent `content`, or no tag at all moves both.
printf '%s\n' "$EVENT_ATTRS" | grep -q 'tag = "type"' ||
	fail "EventMsg is no longer tagged by \"type\" -- payload.type is not the event name"
if printf '%s\n' "$EVENT_ATTRS" | grep -Eq 'content *=|untagged'; then
	fail "EventMsg's fields no longer sit beside its tag -- payload.info has moved"
fi
# And the variant itself keeps the snake_case name: a per-variant rename
# (EventMsg already renames task_started that way) or skip on TokenCount
# changes payload.type with the line above still true.
check_fields "$FILE" 'pub enum EventMsg' 'EventMsg variant' 'TokenCount('
rust_item "$FILE" 'pub struct TokenCountEvent' | grep -q 'pub info: Option<TokenUsageInfo>,' ||
	fail "TokenCountEvent no longer carries info: Option<TokenUsageInfo>"
INFO=$(rust_item "$FILE" 'pub struct TokenUsageInfo')
for field in 'pub total_token_usage: TokenUsage,' 'pub last_token_usage: TokenUsage,'; do
	printf '%s\n' "$INFO" | grep -q "$field" || fail "TokenUsageInfo lost '$field'"
done
USAGE=$(rust_item "$FILE" 'pub struct TokenUsage')
for field in input_tokens cached_input_tokens cache_write_input_tokens output_tokens reasoning_output_tokens total_tokens; do
	printf '%s\n' "$USAGE" | grep -q "pub $field: i64," || fail "TokenUsage lost '$field: i64'"
done
for st in TokenCountEvent TokenUsageInfo TokenUsage; do
	if grep -B4 "pub struct $st\b" "$FILE" | grep -q 'rename_all'; then
		fail "$st is serialised with rename_all -- the JSON keys have moved"
	fi
done
check_fields "$FILE" 'pub struct TokenCountEvent' TokenCountEvent 'pub info:'
check_fields "$FILE" 'pub struct TokenUsageInfo' TokenUsageInfo 'pub total_token_usage:' 'pub last_token_usage:'
check_fields "$FILE" 'pub struct TokenUsage' TokenUsage 'pub input_tokens:' 'pub cached_input_tokens:' \
	'pub cache_write_input_tokens:' 'pub output_tokens:' 'pub reasoning_output_tokens:' 'pub total_tokens:'
ok "token_count still carries info.total_token_usage and last_token_usage, output_tokens and all"

printf 'PASS [codex contract at %s]\n' "$REF"
