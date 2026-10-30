"""Stop or start this machine's installed service, for the fleet orchestrator.

    python -m tests.fleet.service stop
    python -m tests.fleet.service start

The VM fleet lends this Mac's board to a UTM guest. A port somebody has open
is not a device UTM can take -- the board was passed through by hand on
2026-10-19 only with the daemon stopped first (docs/windows-check.md) -- so
tools/fleet/run.py holds the service stopped for as long as the board is
lent out, and starts it again from a finally.

run.py cannot do this itself: it runs under any Python 3.11, and pc.cli
wants the daemon's dependencies. So it runs this under the inventory's
host_python instead, which has them. Same pc.service_ctl the agent uses, so
the same rules hold: nothing installed, moved or deleted, only stopped and
started.

Exit 0 when it worked, 1 when it did not, 3 when BLINK_SKIP_SERVICE made it a
no-op -- which run.py refuses, because a skipped stop leaves the daemon
holding the port UTM needs, and the handoff would fail looking like UTM's
fault.
"""
import sys

from pc.service_ctl import start_service, stop_service

SKIPPED = 3


def main(argv=None, stop=stop_service, start=start_service):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv not in (["stop"], ["start"]):
        print("usage: python -m tests.fleet.service stop|start",
              file=sys.stderr)
        return 2
    outcome = stop() if argv[0] == "stop" else start()
    print(f"[fleet] {argv[0]} service: {outcome}")
    if outcome.skipped:
        return SKIPPED
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
