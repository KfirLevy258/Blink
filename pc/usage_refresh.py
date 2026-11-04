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

Nothing above RefreshWatch logs. The answer holds the person's figures and
must never reach a log line; RefreshWatch logs the event name only, in the
exact words below. Importing does nothing.
"""

import enum
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields

from . import claude_home, claude_plan, ota, theme, user_config, wsl
from . import idle as idle_probe

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


# How far before the run's start a cache's fetch time may sit and still be
# the run's own: the cache's clock is Claude Code's and its fetch can stamp
# a moment before ours did, but not a minute before.
MOVED_SLACK_S = 5


def due(now, *, enabled, native, board_alive, idle_s, cache_at, other_at,
        state):
    """Whether to ask /usage now: the fallback rule, and nothing else.

    BLINK's own sources first: any other Claude reading younger than
    FRESH_SOURCE_S (the status line in a terminal, Claude Desktop's Code
    tab, chat or Cowork) means no run. No exception for a spend seat: while
    Claude Code is used, the running count already adds what each session
    costs. `idle_s` None is an OS that cannot say, taken as in use (Linux).
    `cache_at` None is no cache of this login's yet, which is due."""
    return bool(
        enabled and native and board_alive
        and state.off_reason == ""
        and (idle_s is None or idle_s < IDLE_LIMIT_S)
        and (other_at is None or now - other_at >= FRESH_SOURCE_S)
        and (cache_at is None or now - cache_at >= REFRESH_AFTER_S)
        and now - state.last_run_at >= spacing(state))


def _cache_at(view):
    """When this login's /usage cache was fetched, or None. The anchor and
    the windows come from one cache (claude_plan._cache), so either will
    do; both are asked because a seat may carry only one of them."""
    stamps = [x.fetched_at for x in (view.anchor, view.windows)
              if x is not None]
    return max(stamps) if stamps else None


def config_view(home):
    """`view` for RefreshWatch: Claude Code's config through a PlanReader
    made for each look.

    Not the bus's reader: that one is used from the serial loop, and a
    PlanReader is not thread-safe. Not one long-lived reader of its own
    either: ConfigFile parses at most once a FIRST_ANSWER_RETRY_S (5 s), so
    the look straight after a run that took under 5 s would be handed the
    view from before it, and every quick, good run would count as a
    failure. A parse a minute on this thread is nothing (11 ms for a
    generated 5.6 MB config, claude_plan.ConfigFile)."""
    return lambda now_epoch: claude_plan.PlanReader(home=home).look(now_epoch)


class RefreshWatch:
    """Asks the person's own Claude Code for /usage, on a thread of its own,
    when nothing fresher exists and someone is at the computer (due).

    Never on the serial loop: a run may take the whole RUN_TIMEOUT_S, and
    the board calls the host lost after 30 s without a pong. Everything it
    is handed is a callable read from this thread: `enabled()` the setting,
    `board_alive()` a timestamp the Bridge keeps, `view(now)` the config
    (config_view), `other_at()` the newest other Claude reading, `home()`
    the claude_home record (a WSL one is never run).

    Log lines are exactly: usage refresh ran / skipped / failed (exit N) /
    failed (timeout) / off (cost) / off (failing), and "failed (<type>)"
    for an exception, by type alone. Never the answer, a figure or any of
    stdout: the answer holds the person's usage."""

    def __init__(self, blink_home, home, enabled, board_alive, view,
                 other_at, now=time.time, idle=None, runner=None):
        self._blink_home = blink_home
        self._home = home
        self._enabled = enabled
        self._board_alive = board_alive
        self._view = view
        self._other_at = other_at
        self._now = now
        # Resolved here, not as default arguments, so a test that replaces
        # the module function is followed.
        self._idle = idle if idle is not None else idle_probe.seconds_idle
        self._runner = runner if runner is not None else wsl._bounded_run
        self._said = None          # the exception type said for this streak
        self._missing = False      # "no claude" said for this streak

    def tick_once(self):
        """One decision and at most one run. Never raises: a refresh is
        never worth the thread, and the thread is never worth the daemon."""
        try:
            self._tick()
        except Exception as e:
            self._say_failure(type(e).__name__)

    def _say_failure(self, name):
        # Once per streak, by type alone, as WslWatch._tick_once: the text of
        # an exception from a run can carry what the run printed.
        if self._said != name:
            self._said = name
            print(f"usage refresh failed ({name})", file=sys.stderr)

    def _tick(self):
        now = self._now()
        if self._enabled() is not True:
            return
        h = self._home()
        if h.kind != claude_home.NATIVE:
            # Before the config is read: a WSL home's is a UNC path, and
            # nothing here may stat one.
            return
        state = load_state(self._blink_home)
        if state.last_run_at > now:
            # The clock went back past the last run. Counted as run just
            # now, as StatusWatch does, so the next is a spacing from here
            # rather than whenever the clock catches up with the old reading.
            state = State(now, state.fails, state.failing_since,
                          state.off_reason)
            save_state(self._blink_home, state)
        ask = dict(enabled=True, native=True,
                   board_alive=bool(self._board_alive()),
                   cache_at=_cache_at(self._view(now)),
                   other_at=self._other_at(), state=state)
        # The idle probe starts a process on macOS, so it is asked only once
        # everything else already says yes.
        if not due(now, idle_s=0.0, **ask):
            return
        if not due(now, idle_s=self._idle(), **ask):
            return
        exe = find_claude(h.user_dir())
        if exe is None:
            if not self._missing:
                self._missing = True
                print("usage refresh skipped", file=sys.stderr)
            return
        self._missing = False
        self._ask(exe, state, now)

    def _ask(self, exe, state, started):
        codes, raised = [], []

        def runner(argv, **kw):
            try:
                r = self._runner(argv, **kw)
            except subprocess.TimeoutExpired:
                raise
            except Exception as e:
                raised.append(type(e).__name__)
                raise
            codes.append(r.returncode)
            return r

        try:
            verdict = run_once(exe, self._blink_home, runner=runner)
        except Exception as e:
            # run_once maps a timeout and an OSError; anything else from the
            # runner is a failed run all the same, counted and spaced.
            verdict = Verdict.FAILED
            raised.append(type(e).__name__)
        if verdict is Verdict.OK:
            # A logged-out Claude Code answers -p /usage with a zero-cost
            # success too, and writes no cache (measured 2026-11-05). Only a
            # cache fetched by this run makes it a refresh.
            moved = _cache_at(self._view(self._now()))
            if moved is None or moved < started - MOVED_SLACK_S:
                verdict = Verdict.FAILED
        new = record(state, verdict, started)
        save_state(self._blink_home, new)
        if new.off_reason and not state.off_reason:
            # So `blink status` and `blink config` say off as well.
            try:
                user_config.put(self._blink_home, SETTING, False)
            except (OSError, user_config.Unreadable):
                pass                # the state file's off_reason holds it off
            print(f"usage refresh off ({new.off_reason})", file=sys.stderr)
        elif raised or (verdict is Verdict.FAILED and not codes):
            # No exit code: the runner raised (run_once maps an OSError to
            # FAILED, and so does the except above), or the run folder could
            # not be made, which is an OSError too.
            self._say_failure(raised[-1] if raised else "OSError")
            return
        elif verdict is Verdict.OK:
            print("usage refresh ran", file=sys.stderr)
        elif verdict is Verdict.TIMEOUT:
            print("usage refresh failed (timeout)", file=sys.stderr)
        else:
            print(f"usage refresh failed (exit {codes[-1]})",
                  file=sys.stderr)
        self._said = None

    def start(self):
        """Run tick_once() every TICK_S on a daemon thread, for the
        process's life."""
        t = threading.Thread(target=self._run, name="blink-refresh",
                             daemon=True)
        t.start()
        return t

    def _run(self):
        while True:
            self.tick_once()
            time.sleep(TICK_S)
