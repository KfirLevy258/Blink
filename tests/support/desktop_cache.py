"""Synthetic Claude Desktop usage caches (plan-usage-history.json).

Generated, never captured: the real file sits beside the owner's account, and
a captured copy carries their org id and their usage history into a public
repository. Shared by tests/pc/test_claude_desktop.py and tests/test_cli.py so
the provider's verdict and the `blink status` row it drives are tested against
the same shapes; two copies had already drifted apart once (review,
2026-10-25).

Local builders, not imports from pc.providers.claude_desktop, so a fixture
cannot agree with the parser about a wrong layout.
"""
import json

NOW = 1_787_700_000.0
T_MS = int(NOW * 1000)


def doc(samples, version=2):
    return json.dumps({"version": version, "samples": samples})


def sample(t_ms, fh=10, sd=20):
    return {"t": t_ms, "org": "org-1", "u": {"fh": fh, "sd": sd}}


# The only shapes that may be called "not filled yet": the app's own file
# before its first sample.
UNFILLED = {
    "an empty object": "{}",
    "version 2 with no samples": json.dumps({"version": 2, "samples": []}),
}

# Valid JSON, but not a layout a frame can be read out of. The app always
# writes JSON, so these are what a real format change looks like, and calling
# any of them "empty" hid it.
NOT_UNFILLED = {
    "null": "null",
    "a bare list": "[]",
    "a string": json.dumps("hello"),
    "version 2 samples with renamed keys": json.dumps(
        {"version": 2, "samples": [{"t": T_MS, "u": {"five": 1, "week": 2}}]}),
    "version 2 samples with t in seconds": json.dumps(
        {"version": 2, "samples": [{"t": int(NOW), "u": {"fh": 1, "sd": 2}}]}),
    "version 3 with a new array": json.dumps(
        {"version": 3, "readings": [{"t": T_MS, "fh": 1, "sd": 2}]}),
    "version 3 with no samples": json.dumps({"version": 3, "samples": []}),
    "no samples but an unknown key": json.dumps(
        {"version": 2, "samples": [], "history": [1]}),
    "samples that is not a list": json.dumps({"version": 2, "samples": {}}),
    "a version that is true": json.dumps({"version": True, "samples": []}),
}

# Not JSON, or not text at all: a torn write, a UTF-16 file, plain garbage,
# and nesting deep enough that json.loads raises RecursionError -- neither a
# ValueError nor an OSError, so it went out of every except clause that
# read this file and took `blink status` down with it (review, 2026-10-25).
# A million deep, not ten thousand: the depth json gives up at is the
# interpreter's to choose, and review pointed out that one allowing more
# than ten thousand would parse the old fixture and test nothing.
GARBLED = {
    "truncated": '{"version": 2, "samples": [',
    "not utf-8": b"\xff\xfe\x00garbage",
    "utf-16": json.dumps({"version": 2, "samples": []}).encode("utf-16"),
    "torn mid-character": '{"version": 2, "x": "é'.encode("utf-8")[:-1],
    "nested a million deep": "[" * 1_000_000 + "]" * 1_000_000,
}


def as_bytes(content):
    return content if isinstance(content, bytes) else content.encode("utf-8")
