"""Every text file this program opens is opened as UTF-8, by name.

Python's default text encoding is the platform's: UTF-8 on macOS and Linux,
the ANSI code page on Windows. On a Hebrew Windows 10 that is cp1255, and the
status line payload -- which carries the transcript path, and so the user's
name -- raised UnicodeDecodeError on the first byte outside the code page.
That is a ValueError, the reader treated it as "no data", and a board on that
machine never showed a figure (2026-08-29, found with the file sitting there
fresh and correct). Nothing on a developer's Mac can reproduce it, so this
test reads the source instead of running it.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
FILES = [ROOT / "claude_usage_bridge.py", ROOT / "blink_main.py",
         *sorted((ROOT / "pc").rglob("*.py"))]

# A call to the builtin: not urlopen(), not Path.open(), not a def.
OPEN_CALL = re.compile(r"(?<![\w.])open\(")


def _calls(src):
    for m in OPEN_CALL.finditer(src):
        if "#" in src[src.rfind("\n", 0, m.start()) + 1:m.start()]:
            continue                         # a comment talking about open()
        depth, i = 0, m.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        yield src.count("\n", 0, m.start()) + 1, src[m.start():i + 1]


def test_every_text_open_names_its_encoding():
    bad = []
    for f in FILES:
        for line, call in _calls(f.read_text(encoding="utf-8")):
            if re.search(r"""['"][rwa]?b['"]""", call):
                continue                         # bytes: no decoding at all
            if "encoding=" not in call:      # utf-8, or ascii on purpose
                bad.append(f"{f.relative_to(ROOT)}:{line}: {call}")
    assert not bad, "text opened in the platform's code page:\n" + "\n".join(bad)
