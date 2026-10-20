"""OpenCode's own token counts, against a budget the person sets.

For the desk the OpenCode provider was written for and could not serve:
OpenCode against a SELF-HOSTED model, where there is no GLM Coding Plan and
so no quota endpoint, no limit and no percentage anywhere. Until this, that
desk got a page with blank rings (ingest.IngestionBus._blank_poll) -- the
light worked and the two dials said `--%` for good. The person knows what
their own server can take; OpenCode knows what it has sent it. A budget
joins the two.

WHAT IS READ
------------
Token counts and provider ids, from OpenCode's own message table, and
nothing else: never a message's text, never a model's reply, never a file
name. The columns are picked out with SQLite's json_extract where the
stdlib's SQLite has it (every build this ships with: 3.38 made JSON1
built in); where it does not, the row's JSON is parsed here and dropped
the same moment, and the same few fields are all that survive the call.
Nothing read is ever logged -- a failure is reported as one short category
(ERR_DB), as the plan fetch reports its own.

Two schemas, because OpenCode is mid-rewrite:

  - 1.x (measured, 1.19.3): table `message`, one row per message, the
    message as JSON in `data` -- role, providerID, modelID,
    tokens{input, output, reasoning, cache{read, write}} and, on 1.19.3,
    tokens.total, which is the sum of the other five (checked against a
    real reply: 1034 + 49 + 7 + 200 + 0 = 1290).
  - 2.0 (dev branch, read at packages/core/src/database/schema.gen.ts and
    session/stats.ts): table `session_message` with the role in its own
    `type` column, the provider at $.model.providerID and the same token
    fields. Its own stats query leaves out the messages a FORKED session
    copied from its parent (fork_session_id set, message older than the
    session); counting them would count one reply twice, so the same join
    is made here.

Both tables are read wherever they exist, and summed -- see read_rows for
the real 1.19.3 database that made this the rule. 2.0 copies nothing from
`message` into session_message (no migration on its dev branch does), so a
desk upgraded mid-week has its 1.x replies in one table and its 2.0 replies
in the other; a message id found in both is counted once.

The "total" is OpenCode's: input + output + reasoning + cache read + cache
write, the figure `opencode stats` prints. Cache reads are counted although
a server serves them cheaply, because they are what OpenCode reports and
the person setting a budget will check it against `opencode stats`.

WHAT IS COUNTED
---------------
Assistant messages only -- a user message carries no tokens -- and only
those whose provider is NOT a GLM Coding Plan provider (PLAN_PROVIDERS).
Those already have a real quota, read from the plan's own endpoint, and
counting them here as well would put a plan user's paid-for traffic
against a budget meant for their own server.

Two rolling windows that end now: the last five hours and the last seven
days, matching the two rings every other provider fills. A rolling window
has no reset: it never empties at one moment, each reply leaves it exactly
one window after it was made. So under budget there is no countdown and the
board draws `--`. OVER budget there is a moment worth counting down to: the
one at which enough of the oldest replies have left that the window is back
at its budget, if nothing more is used (clear_at). Asked for by the owner
the morning after a test night left the five-hour ring at 132% with
nothing to say when it would come down.

WHEN IT IS READ
---------------
Only when a budget is set: a desk that never ran `blink opencode-budget`
never has this table opened. Then at most once per poll, and in practice
far less: the rows are cached against the database's and the WAL's
(mtime, size), and re-read only when one of them moved AND at least
MIN_REQUERY_S has passed -- OpenCode rewrites its WAL every few hundred
milliseconds while it streams a reply, and the daemon polls every two
seconds. Between reads the cached rows are re-summed against the moving
window, so a message still ages out on time with nothing new read.
"""
import collections
import json
import math
import os
import sys

# The plan providers' ids. Kept in step with opencode.QUOTA_HOSTS by
# tests/pc/test_opencode_budget.py rather than imported from it, because
# this module is imported by pc/providers/opencode.py.
PLAN_PROVIDERS = ("zai-coding-plan", "zhipuai-coding-plan")

# Expanded when read, never at import: tests/conftest.py moves HOME after
# import, and a module-level expanduser would point at the real one.
BUDGET_FILE = "~/.blink/opencode-budget.json"

# The file is a few dozen bytes. A larger one is not ours.
MAX_BUDGET_BYTES = 4096

SESSION_WINDOW_S = 5 * 3600
WEEKLY_WINDOW_S = 7 * 86400

# A budget below this is a typo -- "5h=2" for "5h=2M" -- that would put the
# rings at a thousand percent after one reply. One above the ceiling is not
# a number any server will reach in a week; it is a unit slip the other way.
MIN_BUDGET_TOKENS = 1000
MAX_BUDGET_TOKENS = 10 ** 15

# firmware/src/proto.c reads session_pct and weekly_pct through num(..., -1,
# PCT_MAX) with PCT_MAX 1000.0, and a value OUTSIDE that range is ignored
# whole -- the ring keeps whatever it last drew. A self-hosted model has no
# hard stop, so a budget can be overrun by any amount; held here at the
# board's ceiling, a ring ten times over reads "over" instead of freezing.
PCT_CEILING = 1000.0

# See WHEN IT IS READ. Ten seconds: a reply that finishes now is on the
# rings within ten seconds, and the table is scanned at most six times a
# minute while somebody works. Measured on this Mac (SQLite 3.53): a
# 200,000-row, 90 MB 1.x `message` table -- months of heavy use, with no
# index that starts with time_created -- scans in about 70 ms, so the
# daemon's two-second poll could not afford it every time but can at this
# rate.
MIN_REQUERY_S = 10.0

# A bound on the work one read may do, whatever the table holds. Seven days
# of assistant messages from one desk is thousands of rows, not this.
MAX_ROWS = 500_000

# Without JSON1 the row's whole JSON comes back to be parsed. A 1.x message
# row is a few hundred bytes (the text is in `part`, not here); a larger
# one is not parsed.
MAX_ROW_BYTES = 256 * 1024

# How long a read waits on a lock OpenCode holds. A busy OpenCode means no
# fresh number this poll, not a stalled daemon.
DB_TIMEOUT_S = 0.5

# The one word a failed read is reported in. Not the exception: sqlite3's
# messages can quote a column's value.
ERR_DB = "could not read OpenCode's database"

Budget = collections.namedtuple("Budget", ("session_tokens", "weekly_tokens"))

# Sums for the two windows, plus what `blink status` needs to tell "used
# nothing" from "the server reported nothing": how many counted messages
# fell in the week, and how many of those had a token count at all.
TokenUse = collections.namedtuple(
    "TokenUse", ("session_tokens", "weekly_tokens", "messages", "counted",
                 "session_clear_at", "weekly_clear_at"),
    defaults=(None, None))


# --- the budget file --------------------------------------------------------


def budget_path():
    return os.path.normpath(os.path.expanduser(BUDGET_FILE))


def _budget_value(v):
    """One window's budget from the file, or None. bool is not a number."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(v):
        return None
    v = int(v)
    return v if MIN_BUDGET_TOKENS <= v <= MAX_BUDGET_TOKENS else None


def parse_budget_doc(doc):
    """A Budget from the file's JSON, or None when neither window has one."""
    if not isinstance(doc, dict):
        return None
    b = Budget(_budget_value(doc.get("session_tokens")),
               _budget_value(doc.get("weekly_tokens")))
    return b if (b.session_tokens or b.weekly_tokens) else None


def read_budget(path=None):
    """The budget, or None: no file, garbage, or nothing usable in it.
    Never raises."""
    path = path if path is not None else budget_path()
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BUDGET_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_BUDGET_BYTES:
        return None
    try:
        return parse_budget_doc(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


class BudgetFile:
    """The budget file, re-read only when it changes.

    Keyed on (inode, mtime_ns, size), so `blink opencode-budget` reaches a
    running daemon on its next poll with no restart: the CLI writes through
    os.replace, which gives the file a new inode even when the write lands
    inside the same mtime tick as the last one.
    """

    def __init__(self, path=None):
        self._path = path
        self._key = None
        self._budget = None

    def path(self):
        return self._path if self._path is not None else budget_path()

    def read(self):
        try:
            st = os.stat(self.path())
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            self._key, self._budget = None, None
            return None
        if key != self._key:
            self._budget = read_budget(self.path())
            self._key = key
        return self._budget


def write_budget(budget, path=None):
    """Write the file atomically, 0600. Removes it for an empty budget.

    Temp file, then os.replace, as opencode.record_dirs writes its record:
    the daemon reads this file every poll, and a truncate-in-place would
    hand it an empty file -- no budget, blank rings -- for one poll in a
    hundred.
    """
    path = path if path is not None else budget_path()
    if budget is None or not (budget.session_tokens or budget.weekly_tokens):
        return clear_budget(path)
    doc = {}
    if budget.session_tokens:
        doc["session_tokens"] = int(budget.session_tokens)
    if budget.weekly_tokens:
        doc["weekly_tokens"] = int(budget.weekly_tokens)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, sort_keys=True)
            f.write("\n")
        try:
            os.chmod(tmp, 0o600)     # a stale temp keeps its old mode
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return True


def clear_budget(path=None):
    """Remove the file. True when there was one."""
    try:
        os.remove(path if path is not None else budget_path())
        return True
    except OSError:
        return False


# --- amounts as people type them --------------------------------------------

_SUFFIX = {"": 1, "k": 1000, "m": 1000 ** 2, "g": 1000 ** 3, "b": 1000 ** 3}


def parse_amount(text):
    """"2M" -> 2000000. Decimal suffixes, K, M and G (or B, for billion),
    either case; "1.5M" is allowed, a comma or underscore between digits
    is ignored. Raises ValueError with a sentence fit to print."""
    s = str(text).strip().replace(",", "").replace("_", "").lower()
    unit = s[-1:] if s[-1:] in _SUFFIX and s[-1:] != "" else ""
    digits = s[:-1] if unit else s
    try:
        v = float(digits)
    except ValueError:
        raise ValueError(f"{text!r} is not a token count (try 2M or 500K)")
    if not math.isfinite(v):
        raise ValueError(f"{text!r} is not a token count (try 2M or 500K)")
    n = int(round(v * _SUFFIX[unit]))
    if n < MIN_BUDGET_TOKENS:
        raise ValueError(f"{text} is fewer than {format_tokens(MIN_BUDGET_TOKENS)}"
                         " tokens -- did you mean a K or M after it?")
    if n > MAX_BUDGET_TOKENS:
        raise ValueError(f"{text} is more tokens than any budget needs")
    return n


def format_tokens(n):
    """1234 -> "1.2K", 2000000 -> "2M". Short, for a status line."""
    n = int(n)
    for div, unit in ((1000 ** 3, "G"), (1000 ** 2, "M"), (1000, "K")):
        if n >= div:
            v = n / div
            text = f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else f"{v:.0f}"
            return text + unit
    return str(n)


# --- the database -----------------------------------------------------------


def _count(v):
    """One token field as a non-negative int. Anything else is 0: a field a
    server left out, a null, a string -- none of them is a count."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    if not math.isfinite(v) or v < 0:
        return 0
    return int(v)


def _text(v):
    if isinstance(v, (bytes, bytearray)):
        try:
            return bytes(v).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return v if isinstance(v, str) else None


def message_total(provider, tokens):
    """The total one assistant message counts for, or None when it does not
    count at all (a plan provider's). `tokens` is the message's tokens
    object; anything else -- missing, a number, a string -- is 0."""
    if _text(provider) in PLAN_PROVIDERS:
        return None
    if not isinstance(tokens, dict):
        return 0
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    return sum(_count(v) for v in (
        tokens.get("input"), tokens.get("output"), tokens.get("reasoning"),
        cache.get("read"), cache.get("write")))


def _tokens_object(raw):
    """The tokens object json_extract handed back as JSON text, or None.

    The whole object rather than its five numbers one by one, because
    json_extract turns a JSON `true` into the integer 1, and a server that
    wrote `"reasoning": true` would have been counted a token for it; parsed
    here, the same _count rule applies on both paths."""
    if not isinstance(raw, (bytes, bytearray, str)) or len(raw) > MAX_ROW_BYTES:
        return None
    try:
        doc = json.loads(raw if isinstance(raw, str)
                         else bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


# The SQL, per schema. `?` is the window's start in milliseconds, which is
# what both schemas store time_created in. Filtered on time first: on 2.0
# that is an index (session_message_time_created_idx); on 1.x there is no
# index that starts with time_created, and the scan is bounded by
# MIN_REQUERY_S instead.
_FIELDS = "json_extract({d}, '$.tokens')"

V1_JSON = ("SELECT id, time_created, json_extract(data, '$.providerID'), "
           + _FIELDS.format(d="data") +
           " FROM message WHERE time_created >= ?"
           " AND json_extract(data, '$.role') = 'assistant' LIMIT %d"
           % MAX_ROWS)
V1_RAW = ("SELECT id, time_created, data FROM message"
          " WHERE time_created >= ? LIMIT %d" % MAX_ROWS)

_V2_FROM = (" FROM session_message AS m{join}"
            " WHERE m.time_created >= ? AND m.type = 'assistant'{fork}"
            " LIMIT %d" % MAX_ROWS)
_V2_JOIN = " LEFT JOIN session_v2 AS s ON s.id = m.session_id"
_V2_FORK = " AND (s.fork_session_id IS NULL OR m.time_created >= s.time_created)"


def _v2(select, forks):
    return select + _V2_FROM.format(join=_V2_JOIN if forks else "",
                                    fork=_V2_FORK if forks else "")


V2_JSON_SELECT = ("SELECT m.id, m.time_created,"
                  " json_extract(m.data, '$.model.providerID'), "
                  + _FIELDS.format(d="m.data"))
V2_RAW_SELECT = "SELECT m.id, m.time_created, m.data"


def _tables(con):
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name IN ('message', 'session_message', 'session_v2')").fetchall()
    return {_text(r[0]) for r in rows}


def _has_json1(con):
    try:
        con.execute("SELECT json_extract('{}', '$.a')").fetchone()
        return True
    except Exception:
        return False


def _raw_message(raw, v2):
    """(provider, tokens) from one row's JSON, or None. The rest of the
    message -- whatever else 1.x or 2.0 keeps in it -- goes out of scope
    here, unread past json.loads."""
    if not isinstance(raw, (bytes, bytearray, str)) or len(raw) > MAX_ROW_BYTES:
        return None
    try:
        doc = json.loads(raw if isinstance(raw, str)
                         else bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    if v2:
        model = doc.get("model")
        provider = model.get("providerID") if isinstance(model, dict) else None
    else:
        if doc.get("role") != "assistant":
            return None
        provider = doc.get("providerID")
    return provider, doc.get("tokens")


def _v2_rows(con, tables, json1, since_ms):
    select = V2_JSON_SELECT if json1 else V2_RAW_SELECT
    if "session_v2" in tables:
        try:
            return con.execute(_v2(select, True), (since_ms,)).fetchall()
        except Exception:
            # A session_v2 without the columns 2.0's stats query joins on
            # is a dev build from before them; count without the fork rule
            # rather than not at all.
            pass
    return con.execute(_v2(select, False), (since_ms,)).fetchall()


def read_rows(path, since_ms, json1=None):
    """[(time_created_ms, total)] for the counted messages since `since_ms`.

    BOTH tables, whichever exist, with a message id found in both counted
    once (2.0's row wins). Found end to end with real OpenCode 1.19.3,
    2026-10-19: 1.19.3 already creates an EMPTY session_message table --
    2.0's, carried ahead of the code that fills it -- so the first version
    of this, which preferred session_message wherever it existed, read zero
    tokens on every 1.x desk while every test, built on a fixture with one
    table or the other, passed. Neither version copies the other's rows
    (no migration on dev's branch moves `message` into session_message), so
    the two are summed; the id check is for the case nothing has shown yet,
    a build that writes one message into both.

    Raises on any failure; TokenCounter turns that into ERR_DB. `json1`
    None asks the database; False forces the Python parse, for the test
    that proves the fallback agrees with the SQL.
    """
    from pc.providers import opencode
    since_ms = int(since_ms)
    con = opencode.connect_readonly(path, timeout=DB_TIMEOUT_S)
    try:
        tables = _tables(con)
        if json1 is None:
            json1 = _has_json1(con)
        batches = []
        if "message" in tables:
            batches.append((False, con.execute(V1_JSON if json1 else V1_RAW,
                                               (since_ms,)).fetchall()))
        if "session_message" in tables:
            batches.append((True, _v2_rows(con, tables, json1, since_ms)))
    finally:
        con.close()
    found = {}
    for v2, rows in batches:
        for row in rows:
            mid, t = _text(row[0]), row[1]
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                continue
            if json1:
                total = message_total(row[2], _tokens_object(row[3]))
            else:
                got = _raw_message(row[2], v2)
                if got is None:
                    continue
                total = message_total(*got)
            if total is None:
                continue
            # A row with no readable id cannot be matched, so it cannot be
            # a duplicate either: it counts under a key of its own.
            found[mid if mid is not None else ("row", v2, len(found))] = (
                int(t), total)
    return list(found.values())


def _stamp(path):
    """What says a database may have changed: its own and its WAL's
    (mtime_ns, size). OpenCode's writes land in the WAL between checkpoints
    and opencode.db does not move (measured on 1.19.3)."""
    out = []
    for p in (path, path + "-wal"):
        try:
            st = os.stat(p)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def clear_at(rows, now_epoch, window_s, budget):
    """When the window ending then is back at `budget`, if nothing more is
    used: an epoch, or None when it is at or under budget already (or there
    is no budget).

    Each row leaves exactly window_s after it was made, oldest first, so
    walk them in that order and stop at the first whose leaving brings the
    sum down to the budget. A row stamped in the future counts as made now,
    as in sum_rows.
    """
    if not budget:
        return None
    now_ms = now_epoch * 1000.0
    start = now_ms - window_s * 1000.0
    inside = sorted((min(t, now_ms), total) for t, total in rows
                    if t >= start and total > 0)
    used = sum(total for _t, total in inside)
    for t, total in inside:
        if used <= budget:
            break
        used -= total
        if used <= budget:
            return t / 1000.0 + window_s
    return None


def sum_rows(rows, now_epoch, budget=None):
    """TokenUse over the two windows ending at now_epoch. A row stamped in
    the future -- a clock that stepped -- counts as now, in both. With a
    Budget, also when each window gets back to it (clear_at)."""
    s_start = (now_epoch - SESSION_WINDOW_S) * 1000.0
    w_start = (now_epoch - WEEKLY_WINDOW_S) * 1000.0
    session = weekly = messages = counted = 0
    for t, total in rows:
        if t < w_start:
            continue
        messages += 1
        weekly += total
        if total > 0:
            counted += 1
        if t >= s_start:
            session += total
    if budget is None:
        return TokenUse(session, weekly, messages, counted)
    return TokenUse(
        session, weekly, messages, counted,
        clear_at(rows, now_epoch, SESSION_WINDOW_S, budget.session_tokens),
        clear_at(rows, now_epoch, WEEKLY_WINDOW_S, budget.weekly_tokens))


class TokenCounter:
    """The cached read behind the rings. See WHEN IT IS READ.

    use(paths, now) never raises: a failure is None -- no number, the page
    stays blank -- and one stderr line in ERR_DB's words per change of
    outcome, not one a poll.
    """

    def __init__(self, reader=read_rows, report=True):
        self._reader = reader
        self._report_on = report
        self._cache = {}        # path -> (stamp, read_at, rows)
        self._reported = None

    def use(self, paths, now_epoch, budget=None):
        try:
            rows, ok = [], True
            for p in paths:
                got = self._rows(p, now_epoch)
                if got is None:
                    ok = False
                    break
                rows.extend(got)
        except Exception:
            ok = False
        self._report(None if ok else ERR_DB)
        if not ok:
            return None
        return sum_rows(rows, now_epoch, budget)

    def _rows(self, path, now_epoch):
        stamp = _stamp(path)
        hit = self._cache.get(path)
        if hit is not None:
            old_stamp, read_at, rows = hit
            fresh = 0 <= now_epoch - read_at < MIN_REQUERY_S
            if old_stamp == stamp or fresh:
                return rows
        try:
            rows = self._reader(path, (now_epoch - WEEKLY_WINDOW_S) * 1000.0)
        except Exception:
            self._cache.pop(path, None)
            return None
        # The stamp from BEFORE the read: a write that lands during it
        # changes the stamp from this one, and so is read next time.
        self._cache[path] = (stamp, now_epoch, rows)
        return rows

    def _report(self, err):
        if err == self._reported:
            return
        if self._report_on:
            if err is not None:
                print(f"[opencode] token count: {err}", file=sys.stderr)
            elif self._reported is not None:
                print("[opencode] token count is being read again",
                      file=sys.stderr)
        self._reported = err


def percent(used, budget):
    """used / budget as a percentage held at PCT_CEILING, or None when that
    window has no budget."""
    if not budget:
        return None
    return min(PCT_CEILING, 100.0 * used / budget)


def no_counts(use):
    """True when OpenCode answered in the week and recorded no tokens for
    any of it: the server is not returning usage (measured, 1.19.3 against
    a server that omits it: every field 0)."""
    return use is not None and use.messages > 0 and use.counted == 0
