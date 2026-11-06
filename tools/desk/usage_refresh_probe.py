"""Read-only looks for tools/desk/usage-refresh-mac.sh, and its one run.

The desk check of the opt-in /usage refresh (plan Task 9) has to read what
the daemon reads, the way the daemon reads it: the idle time from
pc.idle, the cache's fetch time through pc.usage_refresh's own config_view
and _cache_at (which also drops a cache of another login), the state from
usage_refresh.load_state, the claude it would run from locate_claude. A
second, hand-written parser in the shell script would measure something
close to the daemon and call it the daemon.

What it prints, and nothing else:
  - numbers (epoch seconds, idle seconds, counts) and the words the state
    file itself uses for a reason ("cost", "failing");
  - paths: the claude executable, the transcripts folder, the serial port's
    device name (found from USB ids; the port is never opened, the daemon
    holds it, and no serial byte is ever read here).
Never a token, never any other field of ~/.claude.json than
cachedUsageUtilization.fetchedAtMs and whether its accountUuid matches the
login's, never a usage figure, never anything a /usage run printed:
run_once returns a verdict and keeps the answer to itself.

`run-once` is the one command here that does something: one real
`claude -p /usage` through usage_refresh.run_once, with the claude_home
record, exactly as RefreshWatch makes it. It is the fallback for a desk
whose idle guard cannot be satisfied with nobody at the keyboard, and it
writes no state file: RefreshWatch's spacing and guards are untouched.

Standard library and our own pc/ only (pc/ is stdlib only, CLAUDE.md).
Run with PYTHONDONTWRITEBYTECODE=1 and -B, as the script does: it imports
pc/ from the tree. Importing this file does nothing.
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# Same table as tools/burn.sh's port finder: CH340 variants, CP210x, FTDI,
# and Espressif's own USB id.
KNOWN = {(0x1A86, 0x7523), (0x1A86, 0x7522), (0x1A86, 0x5523),
         (0x10C4, 0xEA60), (0x0403, 0x6001)}


def _pc():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from pc import claude_home, idle, statusline_source, usage_refresh, cli
    return claude_home, idle, statusline_source, usage_refresh, cli


def _int(v):
    return "-" if v is None else str(int(v))


def cmd_port():
    """The board's device name, "" for none, MANY:a,b for several. Lists
    ports by USB id; opens nothing (a plain open is safe on this board,
    but the daemon holds the port and the check has no business on it)."""
    from serial.tools import list_ports
    hits = [p.device for p in list_ports.comports()
            if (p.vid, p.pid) in KNOWN or p.vid == 0x303A]
    print(hits[0] if len(hits) == 1
          else ("" if not hits else "MANY:" + ",".join(hits)))
    return 0


def cmd_claude():
    """Where this terminal finds claude, by the CLI's own search (the PATH
    of the shell that started the script, then the fixed folders)."""
    _, _, _, usage_refresh, cli = _pc()
    exe = usage_refresh.find_claude(cli._home())
    if not exe:
        return 1
    print(exe)
    return 0


def cmd_home():
    """kind, then the transcripts folder, one per line."""
    claude_home, _, _, _, _ = _pc()
    h = claude_home.current()
    print(h.kind)
    print(h.transcripts)
    return 0


def _raw_cache(claude_home):
    """(fetchedAtMs as epoch seconds or None, whether the cache names the
    login's accountUuid). The file is parsed whole in memory, as the daemon
    does; only these two facts leave this function."""
    from pc import claude_plan
    try:
        with open(claude_plan.config_path(claude_home.current()),
                  encoding="utf-8") as f:
            doc = json.load(f)
        cache = doc.get("cachedUsageUtilization")
        acct = doc.get("oauthAccount")
        if not isinstance(cache, dict):
            return None, False
        ms = cache.get("fetchedAtMs")
        at = ms / 1000 if isinstance(ms, (int, float)) and \
            not isinstance(ms, bool) else None
        mine = (isinstance(acct, dict) and bool(acct.get("accountUuid"))
                and cache.get("accountUuid") == acct.get("accountUuid"))
        return at, mine
    except Exception:
        return None, False


def cmd_sample():
    """One line, space separated, "-" for none:
    idle_s cache_at raw_cache_at account_match statusline_mtime enabled
    off_reason fails last_run_at missing recorded_exe(0/1)."""
    claude_home, idle, statusline_source, usage_refresh, cli = _pc()
    now = time.time()
    blink_home = cli.blink_home()
    try:
        cache_at = usage_refresh._cache_at(
            usage_refresh.config_view(claude_home.current)(now))
    except Exception:
        cache_at = None
    raw_at, mine = _raw_cache(claude_home)
    try:
        sl = os.stat(statusline_source.payload_path(
            claude_home.native())).st_mtime
    except OSError:
        sl = None
    on = usage_refresh.enabled(blink_home)
    st = usage_refresh.load_state(blink_home)
    print(" ".join([
        _int(idle.seconds_idle()),
        _int(cache_at),
        _int(raw_at),
        "1" if mine else "0",
        _int(sl),
        "-" if on is None else ("1" if on else "0"),
        st.off_reason or "-",
        str(int(st.fails)),
        _int(st.last_run_at),
        "1" if st.missing else "0",
        "1" if usage_refresh.recorded_claude(blink_home) else "0",
    ]))
    return 0


def cmd_board():
    """What the daemon last recorded of the board, in ~/.blink/board.json:
    fw, whether its caps list "ask" (1/0), and the file's mtime. Written by
    the daemon from the board's hello, so a fresh mtime with "ask" in it is
    a board that booted the new firmware and was heard by the daemon. The
    board id and port are in the file too and are not printed."""
    _, _, _, _, cli = _pc()
    path = os.path.join(cli.blink_home(), "board.json")
    try:
        mtime = os.stat(path).st_mtime
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        caps = doc.get("caps") if isinstance(doc, dict) else None
        fw = doc.get("fw") if isinstance(doc, dict) else None
    except (OSError, ValueError):
        print("- 0 -")
        return 0
    ask = isinstance(caps, list) and "ask" in caps
    fw = fw if isinstance(fw, str) and fw and " " not in fw else "-"
    print(f"{fw} {'1' if ask else '0'} {int(mtime)}")
    return 0


def cmd_run_once(exe):
    """One run, as RefreshWatch makes it. Prints: verdict started_at
    cache_at_after. The verdict is usage_refresh's own (ok, cost, failed,
    timeout); "cost" means the run used the model."""
    claude_home, _, _, usage_refresh, cli = _pc()
    h = claude_home.current()
    if h.kind != claude_home.NATIVE:
        print("failed - -")
        return 1
    started = time.time()
    verdict = usage_refresh.run_once(exe, cli.blink_home(), home=h)
    try:
        after = usage_refresh._cache_at(
            usage_refresh.config_view(claude_home.current)(time.time()))
    except Exception:
        after = None
    print(f"{verdict.value} {int(started)} {_int(after)}")
    return 0


def main(argv):
    if not argv:
        print("usage: usage_refresh_probe.py port|claude|home|sample|"
              "board|run-once EXE", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "port":
        return cmd_port()
    if cmd == "claude":
        return cmd_claude()
    if cmd == "home":
        return cmd_home()
    if cmd == "sample":
        return cmd_sample()
    if cmd == "board":
        return cmd_board()
    if cmd == "run-once" and len(argv) == 2:
        return cmd_run_once(argv[1])
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
