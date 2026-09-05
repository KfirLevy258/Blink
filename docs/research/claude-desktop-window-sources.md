# Where Claude Desktop keeps the usage windows

Measured on the owner's desk, 2026-09-05, by launching Claude Desktop and
driving two turns through it while watching every store it writes. This
supersedes the survey quoted in `pc/providers/claude_desktop.py`, whose burn
rate section states that no reset timestamp is persisted anywhere on disk.
That was true of the build it was written against. It is not true now.

This document exists because the obvious reading of these stores is wrong in
a specific, expensive way, and the wrong reading is the one a competent
engineer reaches first.

## The problem

A customer who uses only the Claude Desktop app has no Claude Code status
line, and the status line is the only source that carries all four values we
want: five-hour percentage and reset, seven-day percentage and reset. Today
that customer gets percentages from `plan-usage-history.json` and no
countdown at all, which is why `session_burn_pph` exists.

## What is actually on disk

Three stores, and they are not interchangeable.

### 1. `plan-usage-history.json` -- shipped, unchanged

`~/Library/Application Support/Claude/plan-usage-history.json`

Percentages only (`u.fh`, `u.sd`), `t` in MILLISECONDS, no reset timestamps.
Already read by `pc/providers/claude_desktop.py`. Nothing here changes.

One correction to the cadence comment there: the intervals are not reliably
900 s. Measured over 173 samples spanning 13.5 days, ordinary intervals sit
at 900 s but real gaps of 4365 s and 3432 s appear whenever the app is
closed, and the newest sample was 22 minutes old with the app shut. Treat
900 s as the cadence WHILE RUNNING and nothing more.

### 2. Local Storage -- the five-hour window, and the one to build on

`~/Library/Application Support/Claude/Local Storage/leveldb/`

A Chromium Local Storage LevelDB. The logical key contains
`ochre_heron_tide.<organization-uuid>`; the value is JSON:

```json
{"resetsAt": 1788628199, "utilization": 0.06, "prevUtilization": 0.05,
 "observedAt": 1788613507.705, "atWall": false, "fired": [],
 "shown": false, "shownCowork": false}
```

`resetsAt` is Unix SECONDS. `utilization` is a fraction, not a percentage.
`observedAt` is Unix seconds with fractional precision and is the record's
own idea of when it was taken -- use it, not the file's mtime.

**Only the five-hour window.** There is no seven-day counterpart. Confirmed
by decoding every table and log in the store.

**This store contains no conversation content.** Its full key inventory is UI
state: onboarding flags, experiment toggles, activation checklists, tip
cooldowns, an analytics queue, an Intercom blob. That property is why this is
the store we build on, and any change that starts reading a different one
needs to re-establish it.

### 3. IndexedDB -- all four values, but Cowork only

`~/Library/Application Support/Claude/IndexedDB/https_claude.ai_0.indexeddb.leveldb/`

Contains `rate_limit_event` records carrying everything we want:

```
rate_limit_info.unifiedWindows.five_hour.{resetsAt, utilization}
rate_limit_info.unifiedWindows.seven_day.{resetsAt, utilization}
```

plus `isUsingOverage`, `overageStatus`, `rateLimitType`, `status`,
`session_id`, `uuid`, `created_at`.

**And it is useless for plain Chat.** This is the trap. Both records on this
machine are stored under IndexedDB keys `cowork:cse_014Yg48bDJ...` and
`cowork:cse_01QjSoaGuX...`. The other thirteen large conversation values in
the store -- the actual chats -- carry no usage record at all. Only two
values in the entire store have one, and both are Cowork.

The measurement that produced them was believed at the time to be plain Chat
and was not. Clicking "New" in the tab labelled "Chat and Cowork" produced a
Cowork session. The discriminator used -- no new `audit.jsonl`, no matching
`local_<uuid>` directory -- proves nothing, because these `cse_` sessions run
in a managed environment and write no local folder either. The `hook_event:
Stop` entries sitting beside the record were the real tell.

**Do not repeat this.** If you need to know whether a record came from Chat
or Cowork, read the IndexedDB KEY, not the value, and not the filesystem
around it.

## What the seven-day reset costs

For a chat-only customer it is genuinely unavailable locally. Three
independent attempts failed:

- **Every LevelDB store.** Local Storage, Session Storage and IndexedDB, both
  immutable tables and write-ahead logs, searched for the field names and for
  the two known epochs as text and as UTF-16. Nothing outside the Cowork
  records.
- **The Cowork audit files.** `local-agent-mode-sessions/**/audit.jsonl`
  carries `unifiedWindows` in only 3 of 218 `rate_limit_event`s, and the
  current `cse_` sessions write no audit file at all.
- **Inferring it from the weekly percentage falling.** The handoff claims
  ~15-minute precision for this. On 13.5 days of real history the two weekly
  drops bracket the boundary to windows of 45 hours and 108 hours, because
  the file only samples while the app runs. It cannot produce a countdown.

Two observed weekly boundaries, 1788328800 and 1788933600, are exactly
604800 s apart and both land on Wednesday 06:00:00Z. So the boundary is
stable enough to roll forward -- once you have one.

## Decisions

**The five-hour countdown comes from Local Storage.** It is plain JSON in a
store with no conversation content, it refreshes within seconds of a turn,
and it serves Chat and Cowork users alike.

**The seven-day reset is an anchor, not a feed.** One number that changes
every 604800 s needs one successful read, ever. Learn it from whatever a
machine happens to have, persist it, roll it forward.

**The anchor learns from any source that publishes an exact weekly reset.**
That includes the Claude Code status line and a legacy Cowork audit file.
Many machines will never need anything else.

**The IndexedDB reader is a last-resort seeder, never a poll.** It runs at
most occasionally, tolerates every failure silently, and exists only for a
machine that has never had a status line or an audit file. Because it reads
a store full of conversations, it carries rules the other sources do not.

**A rolled-forward anchor is published, and withdrawn when contradicted.**
The weekly-percentage drop cannot confirm a boundary -- its resolution is
days -- but it can refute one. If a drop is observed more than 24 h from the
predicted boundary, the anchor is wrong: discard it and stop publishing a
weekly reset until a fresh exact one arrives. An anchor with no corroboration
for eight weeks is also withdrawn.

**The anchor never outranks live data.** Its frame carries `observed_at` set
to when the boundary was ORIGINALLY observed, so `pc/normalizer._pick`, which
ranks by recency per field, prefers any live source automatically. It
contributes only `weekly_resets_at`; its percentages stay UNKNOWN.

## Reading a Chromium LevelDB, correctly

Both stores need the same substrate, and `pc/` is stdlib-only, so this is
hand-rolled. Five things a first attempt gets wrong:

1. **The fresh value is in the `.log`, not the `.ldb`.** Measured: the live
   record sat in `035768.log` while `035769.ldb` was already behind it, and
   earlier the newest table was 3.5 hours stale. A reader that parses only
   immutable tables serves hours-old data that looks current.
2. **`.ldb` data blocks are Snappy-compressed.** Byte-scanning a table finds
   nothing at all. Needs a raw-Snappy decompressor.
3. **A WAL record can cross a 32 KiB block boundary**, which injects a
   7-byte header into the middle of the payload. Scanning raw file bytes
   corrupts exactly those records. Assemble records first, then read values.
4. **Deletions must win.** A tombstone in the log overrides a value still
   physically present in a table, or a deleted record is resurrected.
5. **Never hold the file open.** Chromium compacts and deletes these files
   underneath us; on Windows an open handle without `FILE_SHARE_DELETE` can
   fail the app's own compaction. Open, read, close.

Local Storage values also carry Chromium's own framing: keys are
`_<origin>\x00\x01<key>` and values have a one-byte prefix, `0` meaning
UTF-16LE and otherwise UTF-8.

## If the IndexedDB seeder is ever built

The value format is V8's ValueSerializer. Tags observed and verified:
`"` = one-byte string with a VARINT length (not a single byte), `c` = two-byte
string, `S` = UTF-8, `o`...`{` = object, `N` = double, `I` = Smi as zigzag
varint, `0` = null, `T`/`F` = booleans, `^` = object back-reference. V8 does
not back-reference strings, so field names are always spelled out.

Four traps:

- **Never pattern-anchor.** "Find the field name, read the next number" fails
  the moment a conversation MENTIONS the field name -- and this project's own
  transcripts discuss `resetsAt` and `unifiedWindows`. Walk the tags.
- **`utilization` of exactly 0 is a Smi, not a double.** A decoder that
  requires `N` misses the one reading that says the window just refilled.
- **There are three `resetsAt` per record.** An outer block carrying
  `rateLimitType` and `org_level_disabled_until` precedes `unifiedWindows`.
  Positional pairing mispairs the windows the day one is absent.
- **Values over 65536 bytes are externalised to a blob file**, leaving only a
  reference in LevelDB. The live records are ~45 KB, so a longer session
  crosses it. Detect and skip; do not serve an older record instead.

Format versions on disk today: Blink v21, V8 v15. Chromium is migrating
IndexedDB to a SQLite backing store. Check versions and go quiet when they
change.

## Privacy

`README.md:90` tells customers to email support the tail of
`~/.blink/bridge.log` and promises "nothing in either is a secret". That
promise is true today and every source here must keep it true.

Local Storage and `plan-usage-history.json` contain no conversation content,
so ordinary care suffices. The IndexedDB store does. Any code that reads it
must never write a buffer, a byte range, or a decode failure's surrounding
bytes into a log, an exception message, or a fixture.

Fixtures must be generated by committed code, never captured from a real
machine.

## Confidence

One machine, one macOS build, one plan. The Cowork-only finding rests on the
IndexedDB keys of two records and the absence of a usage record in thirteen
plain-conversation values on the same machine, which is strong for this build
and unverified for any other. The weekly boundary's stability rests on two
observations 604800 s apart.

Raw evidence stays off this repo: the stores hold conversation text.
