"""Claude Code inside a WSL distribution, watched from Windows.

The shims inside the distribution write into a Windows folder the daemon
owns, %USERPROFILE%\\.blink\\wsl-<guid> (claude_home.wsl's drop_dir), so the
2 s poll reads NTFS and never starts or enters the distribution
(docs/plans/enterprise-spend.md, section 4).

Nothing here runs at import (tests/test_import_is_inert.py).
"""
import fnmatch
import os

# How old a shim's temp file must be before it is taken for abandoned. The
# shims write one and rename it in the same breath -- a printf and an mv --
# so a minute is not a write still in flight; it is one killed between the
# two, by Claude Code timing out a status line render or a terminal closing
# on a hook. Shorter than install_statusline.STALE_TEMP_S because those are
# whole settings.json writes; these are renders, many a minute.
SHIM_TEMP_STALE_S = 60

# What the shims name their temps: statusline.json.$$.tmp, and
# $sid.state.$$.tmp under state/ (tools/blink-statusline.sh,
# tools/blink-hook.sh). install_statusline._sweep_stale_temps matches
# <file>.<hex>.blink-tmp beside one settings file, and none of these.
_SHIM_TEMP = "*.tmp"


def _sweep_shim_temps(drop_dir, now):
    """Remove the shims' temp files older than SHIM_TEMP_STALE_S in
    `drop_dir` and its state/ folder.

    Natively ~/.blink is on the machine's own disk, and a leftover there
    costs its size. The drop folder is reached from Linux through the
    automount, where a killed render leaves the temp on NTFS, and nothing
    else on Windows will ever look for it; each is a copy of a status line
    payload, with the session's working directory in it. Never raises: a
    file another render is renaming this instant is that render's, and a
    folder that is not there yet has nothing in it.
    """
    for folder in (drop_dir, os.path.join(drop_dir, "state")):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if not fnmatch.fnmatchcase(e.name, _SHIM_TEMP):
                continue
            try:
                if (e.is_file(follow_symlinks=False)
                        and now - e.stat(follow_symlinks=False).st_mtime
                        > SHIM_TEMP_STALE_S):
                    os.remove(e.path)
            except OSError:
                pass
