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
    rows at all -- nothing in pc/ reads the contents, only the mtime -- or an
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
