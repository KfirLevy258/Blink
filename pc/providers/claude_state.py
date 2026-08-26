"""Execution state: what every live Claude Code session is doing, and its agents.

The handoff document specifies this state machine in terms of things a daemon
cannot see from outside -- "CLI process waiting on stdin", "tool execution with
0% CPU". Both mean finding the right process among several, sampling it, and
inferring intent from a number that is legitimately zero whenever a tool waits
on the network. That is a guess dressed as a measurement.

Claude Code announces every one of these transitions through its hook
interface, so this reads the announcements instead:

    UserPromptSubmit  -> a turn started              -> running
    PreToolUse        -> a tool started              -> running
    PostToolUse       -> a tool finished             -> running
    Notification      -> Claude Code wants the user  -> waiting
    Stop              -> the turn completed          -> idle
    StopFailure       -> the turn died on an API error -> failed
    SessionStart/End  -> the session's lifetime
    SubagentStart/Stop-> one agent's lifetime

`waiting` is authoritative rather than inferred: Notification is the event
Claude Code fires when it needs a human, which is what "is the process blocked
on stdin" was trying to establish indirectly. `failed` earns its own state
because on a usage gauge it is the headline -- StopFailure carries
`error: "rate_limit"` among its causes, and being rate limited is the single
thing this product exists to warn about.

`stuck` is the one state with no event of its own, because being wedged is by
definition the absence of one. It is inferred from silence.

ON DISK
-------
    ~/.clauge/state/<session_id>.state   one JSON slot, newest event wins
    ~/.clauge/state/<session_id>/<agent_id>   one empty file per live agent

One file per session because a single global slot silently misreports the
moment a second terminal exists. One file per AGENT because that makes the
count exact without a lock: two agents starting at once cannot race on a
shared counter, and a stop removes precisely the agent that stopped rather
than decrementing and hoping.
"""
import json
import os
import time

from pc.providers import base

PROVIDER_ID = "claude"
SRC_ID = "cli"

STATE_DIR = os.path.expanduser("~/.clauge/state")

# How long a turn may go silent before the panel calls it stuck.
#
# The document says 60 seconds. That is too twitchy to ship: a test suite, an
# npm install, a docker build or a slow model response all routinely exceed a
# minute while being perfectly healthy, and a red alert that cries wolf on
# every build teaches its owner to ignore the one time it is right. Three
# minutes still catches a genuinely wedged tool well before a human would
# notice, and almost never fires on work that is merely slow.
STUCK_AFTER_S = 180.0

# Past this, assume the session is gone rather than that it has been idle
# since. Also the sweep threshold: a session that ended without SessionEnd
# firing (a killed terminal, a crash) leaves files behind, and nothing else
# will ever collect them.
ABANDONED_AFTER_S = 3600.0

_RUNNING_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse",
                   "SubagentStop", "PreCompact", "PostCompact", "SessionStart")
_IDLE_EVENTS = ("Stop", "SessionEnd")
_WAITING_EVENTS = ("Notification", "PermissionRequest")
_FAILED_EVENTS = ("StopFailure",)

# Worst-first. This is the order the single scalar `state` field collapses to
# when several sessions disagree, and the order is a product decision rather
# than an implementation detail: `failed` outranks `stuck` because a rate
# limit is the one condition this gauge exists to surface, and a wedged tool
# is a distant second.
_SEVERITY = (base.STATE_FAILED, base.STATE_STUCK, base.STATE_WAITING,
             base.STATE_RUNNING, base.STATE_IDLE)


def derive_state(event: str, age_s: float, stuck_after_s=STUCK_AFTER_S) -> str:
    """The execution state implied by the last event and how long ago it was."""
    if age_s < 0:
        # A clock that went backwards -- a laptop waking, an NTP step. Treat
        # it as fresh rather than as a negative age that would sail under
        # every threshold below and report a confident state from a broken
        # measurement.
        age_s = 0.0

    if age_s > ABANDONED_AFTER_S:
        return base.STATE_UNKNOWN

    if event in _IDLE_EVENTS:
        # No age test. A completed turn stays completed; silence after it is
        # the expected condition, not a fault.
        return base.STATE_IDLE

    if event in _FAILED_EVENTS:
        # Also no stuck test. A turn that died on an API error is not wedged,
        # it is finished and unsuccessful, and it stays that way until
        # something else happens.
        return base.STATE_FAILED

    if event in _WAITING_EVENTS:
        # A prompt waiting for a human is not wedged, and it stays waiting
        # for as long as the human takes.
        return base.STATE_WAITING

    if event in _RUNNING_EVENTS:
        return base.STATE_STUCK if age_s > stuck_after_s else base.STATE_RUNNING

    # An event this version does not know. Newer Claude Code, most likely.
    # Silence beats guessing.
    return base.STATE_UNKNOWN


def worst_of(states):
    """The state a single indicator should show for several sessions at once."""
    for s in _SEVERITY:
        if s in states:
            return s
    return base.STATE_UNKNOWN


class ClaudeStateProvider(base.ProviderParser):
    """Execution state only -- this source never carries a usage percentage.

    That matters to the normalizer: a frame with no percentage can never
    become the primary source, so this cannot make a panel look fresher than
    its numbers actually are. It contributes execution fields and nothing else.
    """

    def __init__(self, path=None, now=time.time, stuck_after_s=STUCK_AFTER_S,
                 sweep=True):
        self._dir = path if path is not None else STATE_DIR
        self._now = now
        self._stuck_after = stuck_after_s
        self._sweep = sweep

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def path(self):
        return self._dir

    # --- one session ------------------------------------------------------

    def _read_state(self, path, now_epoch):
        """(state, age) for one session's slot file, or (None, None)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        event = payload.get("event")
        if not isinstance(event, str) or not event:
            return None, None
        try:
            t = float(payload["t"])
        except (KeyError, TypeError, ValueError):
            return None, None
        age = now_epoch - t
        return derive_state(event, age, self._stuck_after), age

    def _count_agents(self, session_dir, now_epoch):
        """Live agents for one session.

        An agent file older than the abandon threshold is swept: its
        SubagentStop never fired, because the session died mid-flight. Without
        this the count only ever grows, and a panel that says "3 agents"
        forever is worse than one that says nothing.
        """
        try:
            names = os.listdir(session_dir)
        except OSError:
            return 0
        live = 0
        for name in names:
            p = os.path.join(session_dir, name)
            try:
                age = now_epoch - os.path.getmtime(p)
            except OSError:
                continue
            if age > ABANDONED_AFTER_S:
                if self._sweep:
                    _unlink(p)
                continue
            live += 1
        return live

    # --- the whole directory ----------------------------------------------

    def scan(self, now_epoch):
        """{state: n_sessions}, total live agents. Sweeps what it finds dead."""
        try:
            entries = os.listdir(self._dir)
        except OSError:
            # No hooks installed, or nothing has happened yet. Both normal.
            return {}, 0

        counts = {}
        agents = 0
        for name in entries:
            if not name.endswith(".state"):
                continue
            sid = name[: -len(".state")]
            state_path = os.path.join(self._dir, name)
            state, age = self._read_state(state_path, now_epoch)

            if state is None or state == base.STATE_UNKNOWN:
                # Unreadable, or so old the session is certainly gone. Collect
                # the whole session rather than leaving a directory that will
                # never be looked at again.
                if age is not None and age > ABANDONED_AFTER_S and self._sweep:
                    _unlink(state_path)
                    _rmtree(os.path.join(self._dir, sid))
                continue

            counts[state] = counts.get(state, 0) + 1
            agents += self._count_agents(os.path.join(self._dir, sid),
                                         now_epoch)
        return counts, agents

    def poll(self, now_epoch):
        counts, agents = self.scan(now_epoch)
        if not counts:
            return []
        state = worst_of(counts)
        if state == base.STATE_UNKNOWN:
            return []
        frame = base.NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID, observed_at=now_epoch,
            state=state,
            n_run=counts.get(base.STATE_RUNNING, 0),
            n_wait=counts.get(base.STATE_WAITING, 0),
            n_stuck=(counts.get(base.STATE_STUCK, 0)
                     + counts.get(base.STATE_FAILED, 0)),
            n_idle=counts.get(base.STATE_IDLE, 0),
            n_agents=agents,
        )
        return [frame]


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _rmtree(path):
    try:
        for name in os.listdir(path):
            _unlink(os.path.join(path, name))
        os.rmdir(path)
    except OSError:
        pass
