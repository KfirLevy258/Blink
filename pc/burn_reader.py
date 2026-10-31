"""Claude Code's output tokens over the last five hours, from its transcripts.

For the burn view (docs/plans/burn-5h.md). An account with no five-hour
window -- verified Ultra, from 2026-10-30 -- has no session percentage to
draw, so the left half of the claude page shows how hard the last five hours
have worked instead: output tokens in twenty quarter-hours. Output tokens and
nothing else. On the desk this was built on, cache reads ran to hundreds of
millions against a couple of million output tokens, and a number that size
means nothing to anyone. Claude Code only: Desktop and claude.ai chats leave
no usage on disk.

WHAT IS READ. Claude Code appends one JSON line per event to a transcript per
session, under ~/.claude/projects, or $CLAUDE_CONFIG_DIR/projects when that
is set. Of each line this reads `type`, `timestamp`, `message.id`,
`requestId`, `message.model` and `message.usage.output_tokens`, and looks at
nothing else. Nothing read here is ever logged, printed or raised -- not a
line, not a slice of one, not a value from one, at any level (CLAUDE.md). A
transcript is somebody's whole working session; what leaves this module is
twenty sums. That includes the OSError a failed open raises, whose text is
the transcript's path and so the customer's project name: it is swallowed,
and the file is simply tried again.

THE SHAPE, measured on the development desk 2026-10-31, counts only: 4401
transcripts, 3.3 GB, in 107 directories -- 3606 session files one level down
and 796 subagent files three levels down
(<project>/<session>/subagents/agent-*.jsonl), and none anywhere else. 150 of
them, 225 MB, had been touched in the last five hours and a quarter, the
largest 66 MB. 11 591 of their 43 110 lines contained "output_tokens", every
one of them an assistant line. 4003 (message.id, requestId) pairs had been
written more than once -- streaming writes a message once per content block
-- always with the same figure, and never in two different files. Timestamps
went backwards in 21 of the 150 files, never on an assistant line.

THE COST BOUND. This runs on the daemon's serial thread, inside the poll
loop, on desks with thousands of transcripts, so every part of it is bounded:

  - The tree is walked at most once every SCAN_EVERY_S, and no deeper than
    MAX_DEPTH: a stat per transcript, 14 ms for the 4401 above.
  - A file is read from where the last read of it stopped, so a poll reads
    what was appended since and nothing else. A file untouched for more than
    FOLLOW_S is not read at all; it is followed from its current end, so a
    session resumed after a day costs what the resumption added.
  - At most READ_BUDGET_BYTES per poll. A cold start owes every file touched
    in the window -- those 225 MB -- and pays the debt off over several
    polls, reporting nothing until it has: the board then shows "--%" for a
    few seconds instead of a total that is still being added up.
  - A line is JSON-decoded only if it contains "output_tokens", which is a
    substring test: 11 591 decodes for those 43 110 lines.
  - Memory: a few numbers per transcript, one entry per message still inside
    the window (5744 on that desk, a heavy day of parallel agents), and
    twenty buckets.

MEASURED 2026-10-31 on a generated tree of that shape -- 4401 transcripts in
the same 107 directories, 150 of them touched, 228 MB -- on the dev Mac at a
load average near 5: a cold start took 7 polls and 296 ms of work in all, the
slowest poll 54 ms. A walk that found nothing new took 10.7 ms, and one after
three sessions had each appended a reply took 11.1 ms and read exactly the
16 245 bytes they added. A poll between walks took 0.1 ms. After the cold
start it held 5.8 MB, 11 702 messages remembered, with a peak of 9.4 MB.
With every subagent file given a directory of its own (1698 directories),
the walk rose to about 32 ms, and os.walk with a stat per file took the
same: the cost is the listing, not the code around it.

The cadence of the LINE -- on change, every 60 s, never within 15 s of the
last -- is pc/bridge's; this only answers "what are the twenty numbers now".
"""
import datetime
import json
import os
import re
import stat
import typing

# The contract's buckets: twenty quarter-hours on epoch boundaries,
# floor(ts / 900) * 900. A quarter-hour of epoch time is a wall-clock
# quarter-hour in every time zone, since every UTC offset in use is a
# multiple of fifteen minutes (+5:30 and +5:45 included), so each full local
# hour is some bucket's left edge and the board can label the hours.
BUCKET_S = 900
BUCKETS = 20

# A file whose last write is older than this holds nothing inside the
# window: five hours of buckets, and a quarter-hour more because a file's
# mtime and the stamps inside it are written by different calls and an
# archive or a backup restore can set the first to anything.
FOLLOW_S = BUCKETS * BUCKET_S + BUCKET_S

# How often the tree is walked, which is the per-file cost of all this. The
# board is sent a change no sooner than 15 s after the last line
# (pc/bridge.BURN_MIN_GAP_S), so a walk more often than that would find news
# nobody could be told.
SCAN_EVERY_S = 15.0

# The most a single poll reads, finishing whatever line it is in. Measured
# on the dev desk: reading 227 MB and testing every line for "output_tokens"
# took 205 ms, so this is about 30 ms of the serial loop. The generated tree
# in the module docstring cleared its cold start in seven polls -- twelve
# seconds of fast ticks -- the slowest 54 ms.
READ_BUDGET_BYTES = 32 * 1024 * 1024

# One read() call. Lines are cut out of each chunk, so memory follows this
# and not the file.
CHUNK_BYTES = 1024 * 1024

# A line longer than this is passed over rather than held: it can only be
# held whole to be parsed, and the largest a transcript writes are tool
# results, which carry no usage. A line passed over costs its tokens and
# nothing else.
MAX_LINE_BYTES = 16 * 1024 * 1024

# How many failed opens in a row a transcript may hold the view back for. A
# transient failure gets past this, and the sum waits for it. A file that can
# never be opened -- one left owned by another user, say -- would otherwise
# keep the reader "behind" for good and that desk would never see the view.
# After this many tries the sum goes out without it; it is still tried on
# every poll, and counted once it opens.
OPEN_TRIES = 3

# How many directories down a transcript can be: <project>/<session>/
# subagents/agent-*.jsonl is three, the deepest layout measured. Bounding it
# is what keeps a stray tree under projects/ from becoming the walk.
MAX_DEPTH = 3

SYNTHETIC_MODEL = "<synthetic>"

# The substring a line must contain before it is worth decoding. Every line
# that carried output_tokens on the dev desk was an assistant line.
_NEEDLE = b'"output_tokens"'

# UTC with Z is what Claude Code writes. An explicit offset is honoured, and
# anything else -- no zone at all, above all -- is refused rather than
# guessed at, because a guess is an hour in the wrong bucket.
_STAMP = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?"
                    r"(?:Z|([+-])(\d\d):?(\d\d))\Z")


def transcripts_root() -> str:
    """Where Claude Code keeps its transcripts, worked out now.

    Inside a function and never at import, so tests/conftest.py's sandbox
    applies (tests/test_sandbox.py fails on the other shape). CLAUDE_CONFIG_
    DIR outranks HOME, as it does for Claude Code. A login service does not
    run in the shell that exported it, though, so on a desk that sets it in
    .zshrc the daemon still looks under ~/.claude.

    Joined from the home directory rather than expanded from "~/.claude":
    on Windows expanduser keeps the slash it was given, and the Windows VM
    suite (2026-10-31) measured the root as "...\\dir0/.claude\\projects",
    one forward slash among backslashes.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    base = (os.path.expanduser(base) if base
            else os.path.join(os.path.expanduser("~"), ".claude"))
    return os.path.join(base, "projects")


def _epoch(value):
    """A transcript's timestamp as epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    m = _STAMP.match(value)
    if m is None:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
    try:
        at = datetime.datetime(y, mo, d, h, mi, s,
                               tzinfo=datetime.timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return None                     # 2026-13-01, a leap second
    sign = m.group(7)
    if sign:
        offset = (int(m.group(8)) * 60 + int(m.group(9))) * 60
        at += -offset if sign == "+" else offset
    return at


def _transcripts(root, depth=0):
    """(path, stat) for every regular *.jsonl at most MAX_DEPTH down.

    os.scandir rather than os.walk: the entry knows whether it is a
    directory without another syscall. A directory link is not followed --
    one pointing back up would make the walk endless.
    """
    try:
        entries = os.scandir(root)
    except OSError:
        return
    with entries:
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        yield from _transcripts(e.path, depth + 1)
                elif e.name.endswith(".jsonl"):
                    st = e.stat()
                    if stat.S_ISREG(st.st_mode):
                        yield e.path, st
            except OSError:
                continue                # gone between the listing and now


class PollCost(typing.NamedTuple):
    """What the last poll cost: for tests, and for whoever measures next."""
    walked: bool        # did it walk the tree
    files: int          # transcripts being followed
    read_bytes: int     # bytes read off disk
    behind_bytes: int   # bytes still owed after it (cold start, long pause)
    messages: int       # messages remembered for de-duplication


class _File:
    """What is known about one transcript.

    `offset` is where the next read starts: just after the last complete
    line taken. `looked` is how far the last read got, which is further when
    it ended on a line still being written -- held back, to be read again
    once its newline arrives. A file is owed a read while it is longer than
    `looked`.
    """
    __slots__ = ("offset", "looked", "size", "mtime", "ino", "failures")

    def __init__(self, st, skip):
        self.size, self.mtime, self.ino = st.st_size, st.st_mtime, st.st_ino
        self.offset = self.looked = st.st_size if skip else 0
        self.failures = 0           # opens refused in a row (OPEN_TRIES)


class BurnReader:
    """Twenty quarter-hours of Claude Code output tokens, kept up to date by
    reading only what the transcripts have gained since the last poll."""

    def __init__(self, root=None, read_budget=READ_BUDGET_BYTES,
                 scan_every_s=SCAN_EVERY_S, chunk_bytes=CHUNK_BYTES,
                 max_line_bytes=MAX_LINE_BYTES):
        # None: transcripts_root(), asked on every walk rather than once
        # here -- the reader lives as long as the daemon, and a path fixed at
        # construction is the hole CLAUDE.md describes, one step later.
        self._root = root
        self._budget = read_budget
        self._scan_every = scan_every_s
        self._chunk = chunk_bytes
        self._max_line = max_line_bytes
        self._files = {}            # path -> _File
        # (message.id, requestId) -> [bucket, tokens counted for it]
        self._seen = {}
        self._buckets = {}          # bucket start, epoch s -> output tokens
        self._walked_at = None
        self._found = False         # did the last walk find the root
        self._kept_from = None      # the oldest bucket the last prune kept
        self.last = PollCost(False, 0, 0, 0, 0)

    def root(self):
        return self._root if self._root is not None else transcripts_root()

    def poll(self, now):
        """(q, tokens) for wall-clock `now`, or None when there is nothing
        honest to say yet.

        q is the start of the quarter-hour `now` is in, and tokens the twenty
        buckets oldest first: bucket i covers q - (19 - i) * 900 onwards, in
        whole output tokens. The wire's thousands are pc/protocol.burn's.

        None while there is no projects directory -- twenty zeros would be a
        confident "idle" about transcripts never found -- and while any file
        is still owed a read, for the reason the module docstring gives.
        """
        walked = (self._walked_at is None
                  or now - self._walked_at >= self._scan_every
                  # A clock stepped back would otherwise hold the next walk
                  # off for as long as it moved.
                  or now < self._walked_at)
        if walked:
            self._walk(now)
        q = int(now // BUCKET_S) * BUCKET_S
        oldest = q - (BUCKETS - 1) * BUCKET_S
        self._prune(oldest)
        read = self._drain(oldest)
        behind = sum(f.size - f.looked for f in self._files.values()
                     if f.size > f.looked and f.failures < OPEN_TRIES)
        self.last = PollCost(walked, len(self._files), read, behind,
                             len(self._seen))
        if not self._found or behind:
            return None
        return q, [self._buckets.get(oldest + i * BUCKET_S, 0)
                   for i in range(BUCKETS)]

    def _walk(self, now):
        self._walked_at = now
        root = self.root()
        if not os.path.isdir(root):
            self._found = False
            self._files.clear()
            return
        self._found = True
        cutoff = now - FOLLOW_S
        present = set()
        for path, st in _transcripts(root):
            present.add(path)
            f = self._files.get(path)
            if f is None:
                self._files[path] = _File(st, skip=st.st_mtime < cutoff)
                continue
            # Shorter than what was taken, or another file under the same
            # name (an inode, where the platform's listing gives one): read
            # it again from the start. Nothing is counted twice, because a
            # message already counted is recognised by its id.
            if (st.st_size < f.offset
                    or (st.st_ino and f.ino and st.st_ino != f.ino)):
                f.offset = f.looked = 0
            f.looked = min(f.looked, st.st_size)
            f.size, f.mtime, f.ino = st.st_size, st.st_mtime, st.st_ino
        for gone in [p for p in self._files if p not in present]:
            del self._files[gone]

    def _prune(self, oldest):
        """Forget what has slid out of the window -- its bucket, and the ids
        that let a repeat of one of its messages be recognised. A repeat
        that arrives later is out of the window itself and is dropped."""
        if oldest == self._kept_from:
            return
        self._kept_from = oldest
        for b in [b for b in self._buckets if b < oldest]:
            del self._buckets[b]
        for k in [k for k, v in self._seen.items() if v[0] < oldest]:
            del self._seen[k]

    def _drain(self, oldest):
        """Read what is owed, newest file first, up to the budget."""
        owed = sorted(((f.mtime, p) for p, f in self._files.items()
                       if f.size > f.looked), reverse=True)
        read = 0
        for _, path in owed:
            if read >= self._budget:
                break
            read += self._read(path, self._files[path], self._budget - read,
                               oldest)
        return read

    def _read(self, path, f, budget, oldest):
        """Take the complete lines after f.offset; return the bytes read.

        Stops at the end of the file, or at the first chunk boundary after
        `budget` is spent at which at least one line has been finished. The
        unfinished tail of that chunk is read again next time, at most a
        chunk wasted. What it must not do is stop inside a line it has not
        finished once: a line longer than the budget would then be started
        afresh on every poll and never finished, so it is read to its end
        whatever that costs (MAX_LINE_BYTES bounds the memory, not the time).
        """
        try:
            fh = open(path, "rb")
        except OSError:
            f.failures += 1             # tried again next poll; never said
            return 0
        f.failures = 0
        read = 0
        pos = f.offset                  # where `pending` starts in the file
        pending = bytearray()
        skipping = False
        with fh:
            try:
                fh.seek(pos)
                while True:
                    chunk = fh.read(self._chunk)
                    if not chunk:
                        break
                    read += len(chunk)
                    if skipping:
                        nl = chunk.find(b"\n")
                        if nl < 0:
                            pos += len(chunk)
                            continue
                        pos += nl + 1
                        chunk = chunk[nl + 1:]
                        skipping = False
                    pending += chunk
                    end = pending.rfind(b"\n")
                    if end < 0:
                        if len(pending) > self._max_line:
                            pos += len(pending)
                            pending.clear()
                            skipping = True
                        continue
                    for line in pending[:end].split(b"\n"):
                        self._take(line, oldest)
                    pos += end + 1
                    del pending[:end + 1]
                    if read >= budget:
                        break
            except OSError:
                pass                    # what was taken stands; the rest waits
        f.offset = pos
        f.looked = pos + len(pending)
        return read

    def _take(self, line, oldest):
        """Count one line, if it is an assistant message inside the window
        that has not been counted already."""
        if _NEEDLE not in line:
            return
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            return
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("model") == SYNTHETIC_MODEL:
            return
        usage = msg.get("usage")
        out = usage.get("output_tokens") if isinstance(usage, dict) else None
        # type() and not isinstance(): True is an int to isinstance.
        if type(out) is not int or out < 0:
            return
        mid = msg.get("id")
        if not isinstance(mid, str) or not mid:
            return                      # nothing to recognise a repeat by
        at = _epoch(obj.get("timestamp"))
        if at is None:
            return
        bucket = int(at // BUCKET_S) * BUCKET_S
        if bucket < oldest:
            return
        rid = obj.get("requestId")
        key = (mid, rid if isinstance(rid, str) else None)
        had = self._seen.get(key)
        if had is None:
            self._seen[key] = [bucket, out]
            self._buckets[bucket] = self._buckets.get(bucket, 0) + out
        elif out > had[1]:
            # A repeat carrying a larger figure is the same message, further
            # on: the count becomes that figure, where it was first seen. Not
            # once measured -- every repeat on the dev desk carried the same
            # figure -- but keeping the first would undercount the day one
            # did not, and keeping the sum would double count every day.
            self._buckets[had[0]] = (self._buckets.get(had[0], 0)
                                     + out - had[1])
            had[1] = out
