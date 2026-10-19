"""Synthetic OpenCode data directories and GLM Coding Plan quota replies.

Generated, never captured. A real auth.json holds a live API key and a real
opencode.db holds somebody's conversations; neither belongs in a public
repository, so every byte these tests read is written by the code below.

The reply shapes follow what docs/plans/opencode.md records: the current
credit plan (CREDIT_LIMIT, with currentValue and usage), the plan sold before
2026-07-30 (TOKENS_LIMIT, sometimes with only `percentage`, plus the monthly
TIME_LIMIT tool quota), the HTTP-200 failure envelope (code 1001 for no key,
401 for a bad one), and the server bug that puts a 5-hour reset about ten
hours out (CodexBar #2712). Local constants, not imports from
pc.providers.opencode: a fixture that borrowed the module under test would
agree with it about a wrong unit number.
"""
import json
import os
import sqlite3

# A key that is obviously a key, so a test can search every captured byte for
# it. Not a real one, in any format Z.ai issues.
FAKE_KEY = "blinktestkey.0123456789abcdefSECRET"

SESSION = (3, 5)
WEEKLY = (6, 1)
MONTHLY_TOOLS = (5, 1)


def limit(kind, window, *, current=None, allowance=None, percentage=None,
          reset_s=None, remaining=None):
    """One entry of data.limits. None leaves a key out entirely."""
    unit, number = window
    d = {"type": kind, "unit": unit, "number": number}
    if allowance is not None:
        d["usage"] = allowance
    if current is not None:
        d["currentValue"] = current
    if remaining is not None:
        d["remaining"] = remaining
    elif current is not None and allowance is not None:
        d["remaining"] = allowance - current
    if percentage is not None:
        d["percentage"] = percentage
    if reset_s is not None:
        d["nextResetTime"] = int(reset_s * 1000)
    return d


def reply(limits, *, level="lite", success=True, code=200, msg="Operation successful"):
    return {"code": code, "msg": msg, "success": success,
            "data": {"limits": list(limits), "level": level}}


def credit_plan(now, session_used=71, session_allowance=2000,
                weekly_used=71, weekly_allowance=10000,
                session_reset_in=3 * 3600, weekly_reset_in=4 * 86400):
    """The current plan: both windows CREDIT_LIMIT, matched only by unit."""
    return reply([
        limit("CREDIT_LIMIT", SESSION, current=session_used,
              allowance=session_allowance,
              percentage=int(100 * session_used / session_allowance),
              reset_s=now + session_reset_in),
        limit("CREDIT_LIMIT", WEEKLY, current=weekly_used,
              allowance=weekly_allowance,
              percentage=int(100 * weekly_used / weekly_allowance),
              reset_s=now + weekly_reset_in),
    ])


def legacy_plan(now, session_pct=34, weekly_pct=12, tool_pct=80,
                session_reset_in=2 * 3600, weekly_reset_in=6 * 86400):
    """A plan bought before the relaunch: TOKENS_LIMIT with only a
    percentage, and the monthly tool quota listed FIRST, so a reader that
    went by position would put it on the session dial."""
    return reply([
        limit("TIME_LIMIT", MONTHLY_TOOLS, current=80, allowance=100,
              percentage=tool_pct, reset_s=now + 20 * 86400),
        limit("TOKENS_LIMIT", WEEKLY, percentage=weekly_pct,
              reset_s=now + weekly_reset_in),
        limit("TOKENS_LIMIT", SESSION, percentage=session_pct,
              reset_s=now + session_reset_in),
    ], level="pro")


def ten_hours_ahead(now):
    """The CodexBar #2712 shape: a 5-hour reset stamped ~10 hours out."""
    return credit_plan(now, session_reset_in=10 * 3600)


def failure(code):
    """The HTTP-200 failure envelope: 1001 for no key, 401 for a bad one."""
    return {"code": code, "msg": "Authorization Token Missing" if code == 1001
            else "token expired or incorrect", "success": False}


def encode(doc):
    return json.dumps(doc).encode("utf-8")


# --- the data directory -----------------------------------------------------


def write_auth(data_dir, entries):
    """auth.json with exactly these entries, as OpenCode writes it (0600)."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "auth.json")
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(entries, (str, bytes)):
            f.write(entries if isinstance(entries, str) else entries.decode())
        else:
            json.dump(entries, f)
    os.chmod(path, 0o600)
    return path


def plan_key(provider="zai-coding-plan", key=FAKE_KEY):
    return {provider: {"type": "api", "key": key}}


def make_db(data_dir, name="opencode.db", wal=True, sqlite=True):
    """An OpenCode database, and its WAL when asked.

    A real SQLite file when `sqlite`, made here with sqlite3 and holding no
    rows at all -- the activity clock reads only the mtime, and the
    credential table has its own builder (make_credential_db) -- or an
    empty file otherwise, which is what the presence test needs.
    """
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, name)
    if sqlite:
        con = sqlite3.connect(path)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE IF NOT EXISTS session (id TEXT)")
            con.commit()
        finally:
            con.close()
    else:
        open(path, "wb").close()
    if wal:
        with open(path + "-wal", "wb") as f:
            f.write(b"")
    else:
        # The SQLite inside macOS's own python3 (3.9) leaves the -wal and
        # -shm files behind after the last close, stamped now, where newer
        # builds remove them -- and a -wal is activity to last_activity, so
        # a test that asked for none read the wall clock instead
        # (CI, 2026-10-19). Asked for no WAL means none on disk.
        for extra in ("-wal", "-shm"):
            try:
                os.remove(path + extra)
            except OSError:
                pass
    return path


# OpenCode 2.0's credential table, as dev's migration
# 20260611192811_lush_chimera.ts creates it. Copied out as text, not imported
# from pc/: a fixture that borrowed the reader's idea of the schema would
# agree with it about a wrong column.
CREDENTIAL_TABLE = """
CREATE TABLE `credential` (
  `id` text PRIMARY KEY,
  `integration_id` text,
  `label` text NOT NULL,
  `value` text NOT NULL,
  `connector_id` text,
  `method_id` text,
  `active` integer,
  `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL
);
"""


def credential_key(key=FAKE_KEY):
    """A key credential's value, as 2.0 stores it: Credential.Key, which
    import_legacy_credentials makes from an auth.json {type: "api"} entry."""
    return {"type": "key", "key": key}


def credential(integration="zai-coding-plan", value=None, *, active=None,
               created=1_787_000_000_000, cid=None, label="API key"):
    """One credential row. `value` a dict is stored as its JSON, as drizzle's
    text({mode: "json"}) writes it; str or bytes are stored as given, for
    rows that are not JSON or not text. `active` None is an imported row,
    which has no flag."""
    if value is None:
        value = credential_key()
    return {"id": cid or "cred_%d_%s" % (created, integration),
            "integration_id": integration, "label": label,
            "value": json.dumps(value) if isinstance(value, dict) else value,
            "active": active, "time_created": created,
            "time_updated": created}


def make_credential_db(data_dir, rows=(), name="opencode.db", table=True,
                       journal="WAL"):
    """An OpenCode 2.0 database with these credential rows.

    `table` False leaves the credential table out -- a 1.x database.
    `journal` is WAL by default, as OpenCode runs it; "DELETE" is for the
    lock test, where a writer's exclusive lock shuts readers out entirely.
    """
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, name)
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=%s" % journal)
        con.execute("CREATE TABLE IF NOT EXISTS session (id TEXT)")
        if table:
            con.execute(CREDENTIAL_TABLE)
            for r in rows:
                con.execute(
                    "INSERT INTO credential (id, integration_id, label, value,"
                    " active, time_created, time_updated)"
                    # CAST, so bytes given as a value land as TEXT that is
                    # not UTF-8 -- the row the stdlib's decoder chokes on --
                    # rather than as a BLOB, which it would hand back whole.
                    " VALUES (?, ?, ?, CAST(? AS TEXT), ?, ?, ?)",
                    (r["id"], r["integration_id"], r["label"], r["value"],
                     r["active"], r["time_created"], r["time_updated"]))
        con.commit()
    finally:
        con.close()
    return path


def stamp(path, t):
    os.utime(path, (t, t))


# --- the plugin's slots: exactly tools/blink-hook.sh's format --------------


def slot(state_dir, sid, event, t, pid=None, name=None):
    """One <sid>.state, byte for byte the printf in blink-hook.sh:
    {"event":"%s","t":%s} with ,"pid":N and ,"name":"..." when present."""
    os.makedirs(state_dir, exist_ok=True)
    pidval = ',"pid":%d' % pid if pid is not None else ""
    nameval = ',"name":"%s"' % name if name else ""
    path = os.path.join(state_dir, sid + ".state")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"event":"%s","t":%d%s%s}' % (event, int(t), pidval, nameval))
    stamp(path, t)
    return path


def waiting(state_dir, sid, t):
    path = os.path.join(state_dir, sid + ".waiting")
    open(path, "wb").close()
    stamp(path, t)
    return path


def ended(state_dir, sid, t):
    path = os.path.join(state_dir, sid + ".ended")
    open(path, "wb").close()
    stamp(path, t)
    return path


def agent(state_dir, sid, aid, t):
    d = os.path.join(state_dir, sid)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, aid)
    open(path, "wb").close()
    stamp(path, t)
    return path


# --- messages with token counts: the token budget's input ------------------
#
# Both schemas as their own DDL, copied out as text for the reason
# CREDENTIAL_TABLE gives. 1.x as OpenCode 1.19.3 creates it (read off a
# database 1.19.3 made in an isolated lab, 2026-10-19 -- the DDL only; no
# row of it is here). 2.0 as dev's packages/core/src/database/schema.gen.ts
# creates session_message and session_v2, trimmed of the session columns
# nothing here reads (every NOT NULL one kept that an insert below fills,
# so the reader meets the columns it joins on with their real names).

MESSAGE_TABLE_V1 = """
CREATE TABLE `message` (
  `id` text PRIMARY KEY,
  `session_id` text NOT NULL,
  `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL,
  `data` text NOT NULL
);
CREATE INDEX `message_session_time_created_id_idx`
  ON `message` (`session_id`,`time_created`,`id`);
"""

SESSION_TABLES_V2 = """
CREATE TABLE `session_v2` (
  `id` text PRIMARY KEY,
  `project_id` text NOT NULL,
  `parent_id` text,
  `fork_session_id` text,
  `slug` text NOT NULL,
  `directory` text NOT NULL,
  `version` text NOT NULL,
  `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL
);
CREATE TABLE `session_message` (
  `id` text PRIMARY KEY,
  `session_id` text NOT NULL,
  `type` text NOT NULL,
  `seq` integer NOT NULL,
  `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL,
  `data` text NOT NULL
);
CREATE INDEX `session_message_time_created_idx`
  ON `session_message` (`time_created`);
"""

# Words that must never leave the reader: a test searches stderr, stdout and
# every frame for them. A stand-in for a conversation, not one.
SECRET_TEXT = "blinktest-conversation-SECRET-do-not-log"


def tokens(input=0, output=0, reasoning=0, read=0, write=0, total=True):
    """A tokens object as 1.19.3 writes it, `total` included (the sum of
    the other five, as measured); total=False leaves it out."""
    t = {"input": input, "output": output, "reasoning": reasoning,
         "cache": {"read": read, "write": write}}
    if total:
        t["total"] = input + output + reasoning + read + write
    return t


def v1_message(t_ms, role="assistant", provider="glm-local", tok=None,
               mid=None, session="ses_fixture"):
    """One 1.x `message` row: (id, session_id, time_created, data JSON).
    A text field rides along, so a leak of anything but counts shows."""
    data = {"role": role, "time": {"created": t_ms},
            "summary": {"title": SECRET_TEXT}}
    if role == "assistant":
        data.update({"providerID": provider, "modelID": "glm-5.1",
                     "mode": "build", "cost": 0,
                     "tokens": tok if tok is not None else tokens()})
    return (mid or "msg_%d_%s" % (t_ms, role), session, t_ms,
            json.dumps(data))


def v2_message(t_ms, kind="assistant", provider="glm-local", tok=None,
               mid=None, session="ses_fixture", seq=None):
    """One 2.0 `session_message` row: the role in `type`, the provider at
    $.model.providerID (packages/schema/src/session-message.ts)."""
    data = {"type": kind, "time": {"created": t_ms},
            "content": [{"type": "text", "text": SECRET_TEXT}]}
    if kind == "assistant":
        data.update({"agent": "build",
                     "model": {"providerID": provider, "id": "glm-5.1"},
                     "cost": 0,
                     "tokens": tok if tok is not None else tokens()})
    return {"id": mid or "msg_%d_%s" % (t_ms, kind), "session_id": session,
            "type": kind, "seq": seq, "time_created": t_ms, "data": data}


def make_message_db(data_dir, v1=(), v2=None, sessions=(), name="opencode.db",
                    wal=False):
    """An OpenCode database with these messages.

    `v1` rows go in a 1.x `message` table (always created); `v2` rows, when
    not None, in 2.0's session_message, with `sessions` as session_v2 rows
    (id, fork_session_id, time_created_ms). `wal` False leaves no -wal or
    -shm behind -- a closed OpenCode -- so a test can prove the reader does
    not create them.
    """
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, name)
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(MESSAGE_TABLE_V1)
        con.executemany(
            "INSERT INTO message (id, session_id, time_created, time_updated,"
            " data) VALUES (?, ?, ?, ?, ?)",
            [(m, s, t, t, d) for m, s, t, d in v1])
        if v2 is not None:
            con.executescript(SESSION_TABLES_V2)
            for sid, fork, t in sessions:
                con.execute(
                    "INSERT INTO session_v2 (id, project_id, fork_session_id,"
                    " slug, directory, version, time_created, time_updated)"
                    " VALUES (?, 'prj', ?, 'slug', '/proj', '2', ?, ?)",
                    (sid, fork, t, t))
            for i, r in enumerate(v2):
                con.execute(
                    "INSERT INTO session_message (id, session_id, type, seq,"
                    " time_created, time_updated, data)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (r["id"], r["session_id"], r["type"],
                     r["seq"] if r["seq"] is not None else i,
                     r["time_created"], r["time_created"],
                     json.dumps(r["data"])))
        con.commit()
    finally:
        con.close()
    if not wal:
        # See make_db: macOS's own python3 leaves these behind.
        for extra in ("-wal", "-shm"):
            try:
                os.remove(path + extra)
            except OSError:
                pass
    return path


def write_budget(home, session=None, weekly=None, raw=None):
    """~/.blink/opencode-budget.json under `home`, as the CLI writes it, or
    `raw` bytes/str exactly as given, for the garbage tests."""
    d = os.path.join(home, ".blink")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "opencode-budget.json")
    if raw is None:
        doc = {}
        if session is not None:
            doc["session_tokens"] = session
        if weekly is not None:
            doc["weekly_tokens"] = weekly
        raw = json.dumps(doc)
    with open(path, "wb") as f:
        f.write(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    return path
