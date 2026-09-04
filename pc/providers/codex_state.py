"""Execution state for Codex sessions, out of Codex's own lifecycle hooks.

Codex's rollout log cannot answer "is this session waiting on a person". Its
approval events -- ExecApprovalRequest, ApplyPatchApprovalRequest,
RequestPermissions -- sit in the never-persisted arm of Codex's own persistence
policy, and the real rollouts on this desk contain none of them, including ones
run with approval_policy "on-request". A session sitting on a prompt therefore
looks, in the file, exactly like a session nobody is using.

Its hooks can. Codex 0.150.0 ships a lifecycle hooks system whose event names
are the same words Claude Code uses and whose command hooks are fed the same
session_id and cwd on stdin, so `tools/blink-hook.sh` serves it unchanged --
with one argument telling it to write here instead of into ~/.blink/state.

Which is why this module is thin. The state machine, the slot format, the
waiting marker, the abandonment sweep and the agent counting are all the same
ones Claude's hooks already needed, and they live in claude_state; sharing them
is what keeps a change to the machine from applying to only one of the two
tools. It matters most for the marker: `waiting` lives in its own file rather
than in the slot precisely BECAUSE of a Codex capture -- PreToolUse and
PermissionRequest fired in the same second, from two shim processes each ending
in an atomic mv onto one slot, so the slot cannot be trusted to say `waiting`
(docs/research/codex-hook-contract.md and the ON DISK note in claude_state).
Re-implementing the read here would be re-implementing the one piece of it that
exists for this provider's sake.

What is NOT shared is the directory, and that separation is the whole point: a
Codex session counted out of ~/.blink/state is reported to the board as a
Claude one, on a Claude pip, against a Claude account's limits.

The frames are not built here either. Codex already has a provider, and
pc/normalizer.select_pair shows two providers and drops a third -- so these
counts are unioned onto the existing codex frame in codex_cli rather than
arriving as a provider of their own.
"""
import os

from pc.providers import claude_state

# Expanded when it is used, not here: a module-level expanduser is evaluated
# at import, before a test can move HOME, and the scan below DELETES files
# under this path. Same rule, and same reason, as claude_state.STATE_DIR.
STATE_DIR = "~/.blink/state-codex"


def scan(now_epoch, path=None, sweep=True):
    """({session_id: state}, live agents) for the Codex hook slots.

    `sweep` is forwarded rather than defaulted here, because the caller that
    only wants to LOOK -- `blink status`, a test -- must be able to look
    without collecting: the sweep deletes slots, and a diagnostic that deletes
    what it is diagnosing destroys the evidence somebody ran it to see.

    The names the slots carry are dropped rather than returned. Naming the
    session on the panel is a separate feature with its own rule about when a
    name may be shown at all (claude_state.poll: only when exactly one session
    holds the winning state), and the Codex frame has no label story yet.
    Returning a value with no consumer would invite one to be wired up without
    that rule.
    """
    root = path if path is not None else os.path.expanduser(STATE_DIR)
    states, agents = claude_state.ClaudeStateProvider(
        path=root, sweep=sweep).session_states(now_epoch)
    return {sid: state for sid, (state, _name) in states.items()}, agents
