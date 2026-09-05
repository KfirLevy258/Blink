"""Claude Desktop's five-hour usage record, and only that.

The reasoning about this source lives here rather than in the provider, the
way pc/statusline_source backs pc/providers/claude_cli. The provider is then
a thin adapter and there is one place to look when the app changes shape.

Two facts about the real record, both of which produce a confidently wrong
panel rather than an error if they are missed:

  - `utilization` is a FRACTION. 0.06 is six percent. Read as a percentage it
    draws an empty ring on a window that is filling.

  - `resetsAt` is in SECONDS, and it is frequently in the PAST. The record is
    only rewritten when the app does something, so between a window rolling
    and the next turn the stored percentage describes a window that no longer
    exists. The provider, not this module, decides what to publish then.

This store holds no conversation content -- its other keys are onboarding
flags, experiment toggles, activation checklists and an analytics queue. That
is why it is the store this project reads. Anything that changes it to read a
different one has to re-establish that property first.
"""
import json
import os
import sys

from pc import leveldb

# The logical key is obfuscated and carries the organization uuid. We match
# on the stable middle rather than reconstructing the whole thing, because
# the origin prefix and the uuid are both things we would get wrong.
USAGE_KEY_MARKER = b"ochre_heron_tide"

# Plausible bounds for a record timestamp: 2020-01-01 to 2100-01-01. Same
# guard, and the same reason, as pc/providers/claude_desktop -- one bad
# timestamp in the far future is never stale and beats every real reading,
# forever.
SAMPLE_EPOCH_MIN = 1_577_836_800
SAMPLE_EPOCH_MAX = 4_102_444_800


def store_path() -> str:
    """Where Claude Desktop keeps its Local Storage on this platform.

    Returned whether or not it exists: absence is a normal state and is
    handled by the reader, not by pretending we do not know where to look.
    """
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/Local Storage/leveldb")
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(
            "~\\AppData\\Roaming")
        return os.path.join(appdata, "Claude", "Local Storage", "leveldb")
    return os.path.expanduser("~/.config/Claude/Local Storage/leveldb")


def decode_value(raw: bytes):
    """Chromium's Local Storage value framing: one prefix byte, then text."""
    if not raw:
        return None
    try:
        if raw[0] == 0:
            return raw[1:].decode("utf-16-le")
        return raw[1:].decode("utf-8")
    except (UnicodeDecodeError, IndexError):
        return None


def _valid(rec) -> bool:
    if not isinstance(rec, dict):
        return False
    at = rec.get("observedAt")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    if not (SAMPLE_EPOCH_MIN <= at <= SAMPLE_EPOCH_MAX):
        return False
    return isinstance(rec.get("utilization"), (int, float))


def usage_records(dir_path: str) -> list:
    """Every decodable usage record surviving in the store.

    Deliberately does not go through `leveldb.scan`: scan() collapses to one
    final value per key -- it applies every .ldb file and then lets every
    .log file overwrite on top, unconditionally, regardless of which file
    actually holds the newer write. That is the right default for reading
    general store state, but it means the copy this module needs to compare
    against might already be gone by the time scan() returns.

    So this walks the same two file groups scan() does, with the same public
    per-file readers, but keeps every matching "put" it finds instead of
    reducing them to one -- the tie-break in `newest_record` needs to see
    every surviving copy to pick correctly by observedAt.
    """
    out = []
    try:
        names = os.listdir(dir_path)
    except OSError:
        return out

    for suffix, reader in ((".ldb", leveldb.sst_entries),
                           (".log", leveldb.wal_entries)):
        for name in names:
            if not name.endswith(suffix):
                continue
            try:
                with open(os.path.join(dir_path, name), "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            try:
                entries = reader(data)
            except Exception:
                # An application we do not control is allowed to change its
                # format. One unreadable file must not silence the store.
                continue
            for op, key, value in entries:
                if op != "put" or USAGE_KEY_MARKER not in key:
                    continue
                text = decode_value(value)
                if text is None:
                    continue
                try:
                    rec = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if _valid(rec):
                    out.append(rec)
    return out


def newest_record(dir_path: str):
    """The record with the greatest observedAt, or None.

    By its own timestamp rather than by file order: pc.leveldb.scan documents
    that its ordering is simplified, and this is the tie-break it names.
    """
    best = None
    for rec in usage_records(dir_path):
        if best is None or rec["observedAt"] > best["observedAt"]:
            best = rec
    return best
