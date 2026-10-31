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
twenty sums. That includes the OSError a failed open, read or listing
raises, whose text is the transcript's path and so the customer's project
name: it is swallowed, and the file or directory is simply tried again.
The one thing this module ever says is that a project directory is being
left out of the sum (SAID_LEFT_OUT), in a fixed sentence with no path in it.

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

MEASURED AGAIN 2026-10-31, after the review's fixes, on a second generated
tree of the same shape (237 MB touched) at a load average near 4, the old
reader and the new on the same copy, two runs each: the cold start went
from 248 ms to 277 ms, still 8 polls, the slowest 70 ms either way. About
5 ms of that is the anchors (ANCHOR_BYTES) and about 22 ms the timestamp
parser now shared with pc/cowork_audit, 2.3 us a stamp against 0.8 us for
the copy this module kept (measured by swapping that copy back in). An
append costs 256 bytes
more read for each file it touched: 1254 bytes for three sessions' 486.
Walks and polls between them did not move beyond the noise.

The cadence of the LINE -- on change, every 60 s, never within 15 s of the
last -- is pc/bridge's; this only answers "what are the twenty numbers now".
"""
import json
import os
import stat
import sys
import typing

from pc import protocol
from pc.cowork_audit import iso_to_epoch

# The contract's buckets -- protocol.BURN_BUCKETS quarter-hours of
# protocol.BURN_BUCKET_S on epoch boundaries -- are named in pc/protocol,
# once, for this reader, the scenario that stands in for it, and the encoder.
#
# A file whose last write is older than this holds nothing inside the
# window: five hours of buckets, and a quarter-hour more because a file's
# mtime and the stamps inside it are written by different calls and an
# archive or a backup restore can set the first to anything.
FOLLOW_S = (protocol.BURN_BUCKETS + 1) * protocol.BURN_BUCKET_S

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

# How many failed reads in a row a transcript may hold the view back for,
# where a read fails if the open, a seek or a read() raises before it
# reaches the end of what it set out to read. A transient failure gets past
# this, and the sum waits for it. A file that can never be read -- one left
# owned by another user, one on a disk going bad -- would otherwise keep the
# reader "behind" for good and that desk would never see the view. After
# this many tries the sum goes out without it; it is still tried on every
# poll, and counted once it reads. Only a refused open was counted at first:
# a file that opened and then failed its read() reset the count every poll,
# and the reader returned None for the life of the daemon (review,
# 2026-10-31).
READ_TRIES = 3

# What bridge.log is told when READ_TRIES gives up on a directory, and when
# every directory lists again. Fixed strings: the path of a project
# directory is the customer's project name, and the OSError of a listing
# carries it, so neither is ever part of the line (CLAUDE.md).
SAID_LEFT_OUT = ("[burn] a project directory cannot be listed; the burn view "
                 "goes on without it")
SAID_WHOLE = "[burn] every project directory lists again"

# How many bytes before a file's offset vouch for it: the reader keeps a
# hash of the last ANCHOR_BYTES it took, reads them again ahead of the next
# read, and starts the file over if they differ. Size and inode cannot see
# every rewrite. Claude Code drops a tombstoned message after an API retry
# by opening the transcript r+, truncating it and writing it out again --
# same inode -- and once the next reply lands the file is no shorter than
# what was read, so the read went on from the old offset, inside a line,
# and lost that reply. On Windows, DirEntry.stat() has st_ino 0, so every
# os.replace looked like that too (review, 2026-10-31). An append never
# changes a byte before the offset, so it is never mistaken for a rewrite,
# and a start over counts nothing twice: a message already counted is
# recognised by its (message.id, requestId). 256 bytes is the tail of one
# line, which carries its uuid and timestamp, so two different files agree
# there only by copying each other. It costs one 256-byte read, from the
# page cache, per file per read; see the module docstring for what that
# came to.
ANCHOR_BYTES = 256

# How many directories down a transcript can be: <project>/<session>/
# subagents/agent-*.jsonl is three, the deepest layout measured. Bounding it
# is what keeps a stray tree under projects/ from becoming the walk.
MAX_DEPTH = 3

SYNTHETIC_MODEL = "<synthetic>"

# The substring a line must contain before it is worth decoding. Every line
# that carried output_tokens on the dev desk was an assistant line.
_NEEDLE = b'"output_tokens"'

# A listing that fails with one of these is a directory that has gone --
# deleted between its parent's listing and its own -- not one that cannot be
# read, and it is passed over the way a deleted transcript is.
_GONE = (FileNotFoundError, NotADirectoryError)


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


def _transcripts(root, refused, depth=0):
    """(path, stat) for every regular *.jsonl at most MAX_DEPTH down.

    os.scandir rather than os.walk: the entry knows whether it is a
    directory without another syscall. A directory link is not followed --
    one pointing back up would make the walk endless.

    A directory that exists and cannot be listed, the projects directory or
    any below it, appends to `refused`, and the caller then has nothing
    honest to say, for READ_TRIES walks: the sum without it is a total that
    leaves part of the desk out and is drawn as if it were the whole. The
    listing's OSError used to be swallowed, so an unlistable projects
    directory read as twenty zeros, a confident "idle" (review, 2026-10-31).
    """
    try:
        entries = os.scandir(root)
    except _GONE:
        if depth == 0:
            refused.append(root)        # the root itself: not found at all
        return
    except OSError:
        refused.append(root)
        return
    with entries:
        while True:
            try:
                e = next(entries)
            except StopIteration:
                return
            except OSError:
                refused.append(root)    # the listing broke off part-way
                return
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        yield from _transcripts(e.path, refused, depth + 1)
                elif e.name.endswith(".jsonl"):
                    st = e.stat()
                    if stat.S_ISREG(st.st_mode):
                        yield e.path, st
            except OSError:
                continue                # gone between the listing and now


def _tail(tail, buf, end):
    """The last ANCHOR_BYTES of `tail` followed by buf[:end]."""
    return (tail + bytes(buf[max(0, end - ANCHOR_BYTES):end]))[-ANCHOR_BYTES:]


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
    `looked`. `anchor` is the hash of the ANCHOR_BYTES before `offset`, the
    last ones taken, or None where nothing has been read to vouch for it (a
    file followed from its end). A hash and not the bytes, so that nothing
    of a transcript is held from one poll to the next.
    """
    __slots__ = ("offset", "looked", "size", "mtime", "ino", "failures",
                 "anchor")

    def __init__(self, st, skip):
        self.size, self.mtime, self.ino = st.st_size, st.st_mtime, st.st_ino
        self.failures = 0           # reads failed in a row (READ_TRIES)
        self.restart(st.st_size if skip else 0)

    def restart(self, at):
        """Read on from `at`, with nothing known about what precedes it."""
        self.offset = self.looked = at
        self.anchor = None


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
        # Did the last walk find the root and list every directory under
        # it? Neither is something twenty zeros could honestly stand for.
        self._listed = False
        # Directory -> walks in a row that could not list it, READ_TRIES for
        # directories: see _walk. And whether bridge.log has been told that
        # one of them is being left out, so it is told once.
        self._refusals = {}
        self._left_out_said = False
        self._kept_from = None      # the oldest bucket the last prune kept
        self.last = PollCost(False, 0, 0, 0, 0)

    def root(self):
        return self._root if self._root is not None else transcripts_root()

    def poll(self, now):
        """(q, tokens) for wall-clock `now`, or None when there is nothing
        honest to say yet.

        q is the start of the quarter-hour `now` is in, and tokens the twenty
        buckets oldest first: bucket i covers q - (19 - i) * 900 onwards, in
        whole output tokens, which is what the wire carries too.

        None while there is no projects directory, or one this user cannot
        list -- twenty zeros would be a confident "idle" about transcripts
        never found -- while a directory under it has failed to list for
        fewer than READ_TRIES walks in a row, and while any file is still
        owed a read, for the reason the module docstring gives.
        """
        walked = (self._walked_at is None
                  or now - self._walked_at >= self._scan_every
                  # A clock stepped back would otherwise hold the next walk
                  # off for as long as it moved.
                  or now < self._walked_at)
        if walked:
            self._walk(now)
        q = protocol.burn_quarter(now)
        oldest = q - (protocol.BURN_BUCKETS - 1) * protocol.BURN_BUCKET_S
        self._prune(oldest)
        read = self._drain(oldest)
        behind = sum(f.size - f.looked for f in self._files.values()
                     if f.size > f.looked and f.failures < READ_TRIES)
        self.last = PollCost(walked, len(self._files), read, behind,
                             len(self._seen))
        if not self._listed or behind:
            return None
        return q, [self._buckets.get(oldest + i * protocol.BURN_BUCKET_S, 0)
                   for i in range(protocol.BURN_BUCKETS)]

    def _walk(self, now):
        self._walked_at = now
        root = self.root()
        if not os.path.isdir(root):
            self._listed = False
            self._files.clear()
            return
        cutoff = now - FOLLOW_S
        present = set()
        refused = []
        for path, st in _transcripts(root, refused):
            present.add(path)
            f = self._files.get(path)
            if f is None:
                self._files[path] = _File(st, skip=st.st_mtime < cutoff)
                continue
            if st.st_mtime < cutoff:
                # Gone cold: whatever it gained since the last read is older
                # than the window, so it is followed from its end, the way a
                # file first seen cold is. Only new files were, and a known
                # one kept its offset: after a stretch with no polls -- the
                # board unplugged overnight -- everything appended in the
                # meantime was read though none of it could count, and the
                # line was held back while it was (review, 2026-10-31).
                f.restart(st.st_size)
            elif (st.st_size < f.offset
                    or (st.st_ino and f.ino and st.st_ino != f.ino)):
                # Shorter than what was taken, or another file under the
                # same name (an inode, where the platform's listing gives
                # one): read it again from the start. Nothing is counted
                # twice, because a message already counted is recognised by
                # its id. A rewrite neither of these can see is caught by
                # the anchor (ANCHOR_BYTES) when the file is next read.
                f.restart(0)
            f.looked = min(f.looked, st.st_size)
            f.size, f.mtime, f.ino = st.st_size, st.st_mtime, st.st_ino
        # READ_TRIES, for a directory. One under projects/ that this user
        # cannot list -- a project left owned by root by a `sudo claude` --
        # used to hold poll() at None for the life of the daemon, silently:
        # the files rule had a bound and the directories none (/code-review
        # max, 2026-11-01). A refusal is waited for, as a read's is, and
        # after READ_TRIES walks in a row the sum goes out without what that
        # directory holds; it is still listed on every walk, and counted the
        # moment it lists. The projects directory itself is never given up
        # on: without it there is no sum at all to send short.
        self._refusals = {d: self._refusals.get(d, 0) + 1 for d in refused}
        waiting = [d for d in refused
                   if d == root or self._refusals[d] < READ_TRIES]
        self._listed = not waiting
        left_out = any(d != root and self._refusals[d] >= READ_TRIES
                       for d in refused)
        if left_out != self._left_out_said:
            self._left_out_said = left_out
            print(SAID_LEFT_OUT if left_out else SAID_WHOLE, file=sys.stderr)
        # What a refused directory holds was not listed, which is not the
        # same as gone: forgotten, every file in it touched inside the
        # window would be read again from its start once it lists. The
        # rest of the tree was listed, and what is missing from it is gone.
        under = tuple(os.path.join(d, "") for d in refused)
        for gone in [p for p in self._files
                     if p not in present and not p.startswith(under)]:
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

        Before it reads on from an offset it reads the ANCHOR_BYTES before
        it again, and starts from the beginning of the file if they are not
        the ones it took. A read that raises before it has finished counts
        towards READ_TRIES, whatever raised: the open, a seek, a read() or
        the close. The close sat outside the try, so an OSError from it left
        poll() every time (/code-review max, 2026-11-01); one try now holds
        all four, and nothing raised is ever said.
        """
        read = 0
        pos = f.offset                  # where `pending` starts in the file
        pending = bytearray()
        # The last ANCHOR_BYTES taken, ending at `pos`; None until the read
        # has established them, so a read that fails first leaves the anchor
        # it had.
        tail = None
        skipping = False
        failed = True
        try:
            with open(path, "rb") as fh:
                if pos:
                    k = min(pos, ANCHOR_BYTES)
                    fh.seek(pos - k)
                    tail = fh.read(k)
                    read += len(tail)
                    if len(tail) != k or (f.anchor is not None
                                          and hash(tail) != f.anchor):
                        # Rewritten under the same name: start over.
                        pos, tail = 0, b""
                        fh.seek(0)
                else:
                    tail = b""
                while True:
                    chunk = fh.read(self._chunk)
                    if not chunk:
                        break
                    read += len(chunk)
                    if skipping:
                        nl = chunk.find(b"\n")
                        if nl < 0:
                            pos += len(chunk)
                            tail = _tail(tail, chunk, len(chunk))
                            continue
                        pos += nl + 1
                        tail = _tail(tail, chunk, nl + 1)
                        chunk = chunk[nl + 1:]
                        skipping = False
                    pending += chunk
                    end = pending.rfind(b"\n")
                    if end < 0:
                        if len(pending) > self._max_line:
                            pos += len(pending)
                            tail = _tail(tail, pending, len(pending))
                            pending.clear()
                            skipping = True
                        continue
                    for line in pending[:end].split(b"\n"):
                        self._take(line, oldest)
                    pos += end + 1
                    tail = _tail(tail, pending, end + 1)
                    del pending[:end + 1]
                    if read >= budget:
                        break
            failed = False
        except OSError:
            pass                        # what was taken stands; the rest waits
        f.failures = f.failures + 1 if failed else 0
        f.offset = pos
        f.looked = pos + len(pending)
        if tail is not None:
            f.anchor = hash(tail) if pos else None
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
        # cowork_audit's parser, the codebase's one: UTC with Z is what
        # Claude Code writes, an explicit offset is honoured, and no zone at
        # all is refused rather than guessed at, since a guess is an hour in
        # the wrong bucket. It keeps the fraction of a second this module's
        # own parser dropped, which moves no stamp across a quarter-hour,
        # and it refuses an offset past +23:59, which that one took.
        at = iso_to_epoch(obj.get("timestamp"))
        if at is None:
            return
        bucket = protocol.burn_quarter(at)
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
