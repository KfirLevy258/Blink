"""Seconds since the last keyboard or mouse input, where the OS will say.

None means "this OS cannot say". The refresh runs only while someone is at
the computer, so the caller (usage_refresh.at_computer) does not take None
as present: it falls back to another Claude source having been active inside
IDLE_LIMIT_S, and with neither, nothing runs.
"""
import re
import subprocess
import sys

_HID = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


def _win_ticks():
    """(now_tick, last_input_tick) in ms, or None. ctypes is touched only
    here: tests/test_import_is_inert.py imports every module on every OS."""
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return ctypes.windll.kernel32.GetTickCount(), info.dwTime
    except Exception:
        return None


def seconds_idle(runner=subprocess.run, platform=sys.platform):
    try:
        if platform == "darwin":
            p = runner(["ioreg", "-c", "IOHIDSystem", "-d", "4",
                        "-k", "HIDIdleTime", "-r"],
                       capture_output=True, text=True, timeout=5)
            if p.returncode != 0:
                return None
            m = _HID.search(p.stdout or "")
            return int(m.group(1)) / 1e9 if m else None
        if platform == "win32":
            t = _win_ticks()
            if t is None:
                return None
            tick, last = t
            return ((tick - last) % 2**32) / 1000
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    # Linux: no stdlib source, and X11 and Wayland differ; say nothing.
    return None
