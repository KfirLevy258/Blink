"""Run the person's own `claude -p /usage`, judge the answer, remember it.

Measured 2026-11-04 on a logged-in Claude Code: `claude -p /usage` is a
local slash command. The JSON it prints has num_turns 0, total_cost_usd 0
and an empty modelUsage, and it rewrites `cachedUsageUtilization` in
~/.claude.json, which is what claude_plan reads. Nothing reaches the model,
so nothing is billed against the owner's quota. A logged-out Claude Code also
answers with success (2026-11-05), so a run is only trusted when the cache
moves; that check belongs to the daemon thread, not here.

Why each flag is there:
  --no-session-persistence   no transcript is written for the run.
  --strict-mcp-config + an empty --mcp-config   no MCP server of the
                             person's is started for a command that needs none.
  --settings {"disableAllHooks":true}   none of their hooks fire either.
  never --bare               it drops the subscription login and the
                             command then has nothing to ask.
The run happens in <blink_home>/refresh so the folder-trust and project
files of wherever the daemon started do not matter, and BLINK_REFRESH=1
lets the person's own hooks and statusline tell the run from their work.

The guard is the judge: any sign the model was used (a turn, a cent, a
model in modelUsage) turns the feature off for good, whatever the exit code.

Nothing here logs. The answer holds the person's figures and must never
reach a log line; callers log the event name only. Importing does nothing.
"""

import enum
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, fields

from . import ota, theme, wsl

SETTING = "usage-refresh"

FRESH_SOURCE_S = 15 * 60
REFRESH_AFTER_S = 25 * 60
ASK_EVERY_S = 24 * 3600
ASK_AT_MOST = 3
IDLE_LIMIT_S = 30 * 60
RUN_TIMEOUT_S = 60
BACKOFF_AFTER = 3
BACKOFF_S = 3600
GIVE_UP_S = 24 * 3600
TICK_S = 60

STATE_FILE = "usage-refresh.json"


class Verdict(enum.Enum):
    OK = "ok"
    COST = "cost"
    FAILED = "failed"
    TIMEOUT = "timeout"


def find_claude(home):
    """The claude executable: PATH first, then the installer's own folder,
    which a launchd or Task Scheduler job's short PATH does not include."""
    found = shutil.which("claude")
    if found:
        return found
    name = "claude.exe" if sys.platform == "win32" else "claude"
    local = os.path.join(home, ".local", "bin", name)
    return local if os.path.isfile(local) else None


def command(exe):
    return [exe, "-p", "/usage", "--output-format", "json",
            "--no-session-persistence", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--settings", '{"disableAllHooks":true}']


def _positive(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def judge(returncode, stdout):
    try:
        doc = json.loads(stdout)
        if not isinstance(doc, dict):
            return Verdict.FAILED
        if (_positive(doc.get("num_turns"))
                or _positive(doc.get("total_cost_usd"))
                or doc.get("modelUsage")):
            return Verdict.COST
        if (returncode == 0 and doc.get("type") == "result"
                and doc.get("is_error") is False
                and doc.get("num_turns") == 0
                and doc.get("total_cost_usd") == 0):
            return Verdict.OK
    except Exception:
        pass
    return Verdict.FAILED


def run_once(exe, blink_home, runner=wsl._bounded_run):
    try:
        cwd = os.path.join(blink_home, "refresh")
        os.makedirs(cwd, mode=0o700, exist_ok=True)
        # Read at call time so a test can pose as Windows.
        extra = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        env = dict(os.environ, BLINK_REFRESH="1")
        r = runner(command(exe), timeout=RUN_TIMEOUT_S, cwd=cwd, env=env,
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL, **extra)
    except subprocess.TimeoutExpired:
        return Verdict.TIMEOUT
    except OSError:
        return Verdict.FAILED
    return judge(r.returncode, r.stdout or b"")


@dataclass
class State:
    last_run_at: float = 0.0
    fails: int = 0
    failing_since: float | None = None
    off_reason: str = ""


def _path(blink_home):
    return os.path.join(blink_home, STATE_FILE)


def _read(blink_home):
    try:
        with open(_path(blink_home), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def load_state(blink_home):
    run = _read(blink_home).get("run")
    if not isinstance(run, dict):
        return State()
    try:
        kw = {f.name: run[f.name] for f in fields(State) if f.name in run}
        s = State(**kw)
        if (not isinstance(s.fails, int) or isinstance(s.fails, bool)
                or not isinstance(s.last_run_at, (int, float))
                or not isinstance(s.off_reason, str)
                or not (s.failing_since is None
                        or isinstance(s.failing_since, (int, float)))):
            return State()
        return s
    except (TypeError, ValueError):
        return State()


def save_state(blink_home, state):
    doc = _read(blink_home)
    doc["run"] = asdict(state)
    theme._write_json(_path(blink_home), doc, tries=1)


def record(state, verdict, now):
    s = State(state.last_run_at, state.fails, state.failing_since,
              state.off_reason)
    s.last_run_at = now
    if verdict is Verdict.OK:
        s.fails = 0
        s.failing_since = None
    elif verdict is Verdict.COST:
        s.off_reason = "cost"
    else:
        s.fails += 1
        if s.failing_since is None:
            s.failing_since = now
        if now - s.failing_since >= GIVE_UP_S:
            s.off_reason = "failing"
    return s


def spacing(state):
    return BACKOFF_S if state.fails >= BACKOFF_AFTER else REFRESH_AFTER_S
