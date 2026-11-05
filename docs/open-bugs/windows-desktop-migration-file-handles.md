# The daemon's plain open() can block Claude Desktop's folder move on Windows

**Status:** OPEN, measured 2026-11-05 on the Windows 11 ARM VM. Not a failure
we have seen on a desk: the exposure is real but tiny, and nothing here is
fixed. Raised by the PR 3 review (finding 12).

## Why it matters now

PR 3 makes the daemon read `plan-usage-history.json` and Chromium's leveldb
files inside the Store package's `LocalCache\Roaming\Claude`. That is the folder
Claude Desktop migrates out of. On a friend's PC the marker was written at
23:23:27 and the cache was at `%LOCALAPPDATA%\Claude-Data\<name>` by 23:23:38,
eleven seconds later. The daemon polls every 2 s, so it is reading the folder
while Desktop is moving it.

Both readers use Python's plain `open(path, "rb")`: `read_text` in
`pc/providers/claude_desktop.py` and `_read_whole` in `pc/leveldb.py`. On
Windows that omits `FILE_SHARE_DELETE` (the note in `pc/leveldb.py` says so).
While our handle is open, nobody can delete or rename that file, or rename any
directory above it.

## What fails while a plain open() handle is live

Generated 50 KB file in `...\Roaming\Claude\plan-usage-history.json` under a
temp folder, handle held with `open(f, "rb")`, then the move tried from the
same process. Python 3.11 x64 on the VM.

| Operation | Result |
|---|---|
| `os.rename` file to another folder, same volume | fails, WinError 32 (in use) |
| `os.replace` file to another folder | fails, WinError 32 |
| `os.remove` file | fails, WinError 32 |
| `os.rename` of the parent directory (`Claude` -> `Claude2`) | fails, WinError 5 (access denied) |
| `os.rename` of the grandparent directory | fails, WinError 5 |
| `shutil.move` of the parent directory to another folder | fails, WinError 32 |
| PowerShell `Move-Item` of the parent directory | fails, "being used by another process" |
| PowerShell `Move-Item` of the file | fails, same message |

Every operation fails. There is no case where the move goes through.

Control: the same file opened through `CreateFileW` with share mode 7
(read|write|delete). `os.remove` then succeeds, but renaming the parent
directory **still fails** with WinError 5. So `FILE_SHARE_DELETE` fixes file
moves and deletes only; it does not make a directory rename possible while any
handle is open inside it. Which of these Desktop's migrator uses (per-file
move, or one directory rename) is unknown, see below.

## The exposure window

5000 sequential reads of a generated 50 KB file, timed from the end of
`open()` to the end of `close()` (the span the handle is live):

- median 168 us, mean 272 us, p99 1.2 ms, worst 4.8 ms
- the `read()` call is most of it: median 119 us

At one read per 2 s that is about 0.008 percent of the time with a handle open
(median). A leveldb poll opens several files one after another, each held for
the same sort of time, so the daemon's total is a small multiple of that. A
5 s hot loop with a mover racing it (no sleeps, far harsher than the daemon)
saw the mover fail on most attempts, so a collision is possible whenever the
two overlap, but the daemon overlaps rarely.

Both readers already close at once (`_read_whole` is written that way on
purpose), so the hold cannot be shortened by reading in one shot; it already
is.

## What is still unknown

- How Desktop's migrator reacts when a move fails: a retry, a skip, a copy
  fallback, or giving up and leaving the cache behind. If it retries, a 170 us
  collision is harmless. If it gives up once, a collision is a stuck
  migration. Not measurable without touching Desktop, which this test did not.
- Whether the migrator moves files one by one or renames the directory. This
  decides whether `FILE_SHARE_DELETE` would help at all.
- Whether antivirus or the search indexer already holds the same handles for
  longer than we do. Likely, which would make our share of the risk small.
- No collision has been observed on the friend's PC; the 11 s migration there
  happened with the PR 3 code not yet running.

## Cheap mitigations

1. Do nothing. The window is about 0.01 percent per read.
2. Stop reading the Store package folder once the migration marker exists, or
   while its mtime is under a few seconds old. This removes the overlap with
   the 11 s migration rather than shrinking it, and needs no handle tricks.
   Best option if one is wanted.
3. Open with `FILE_SHARE_DELETE` using stdlib only: `ctypes.windll.kernel32
   .CreateFileW` with share mode 7, then `msvcrt.open_osfhandle` and
   `os.fdopen`. Works (the control above), but does not cover a directory
   rename, adds ctypes code to a hot path, and is hard to test off Windows.
   Not recommended.

No code was changed.
