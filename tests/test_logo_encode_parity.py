"""pc/logo_encode (standard library) against tools/encode_logo.py (NumPy and
Pillow): the same generated inputs must give the same bytes, padding included.
Skips without NumPy and Pillow, which only the test venv has."""
import os
import subprocess
import sys

import pytest

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")

from pc import logo_encode as le  # noqa: E402
from tests.support import gen_gif as gg  # noqa: E402
from tests.support import gen_logo_frames as lf  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "encode_logo.py")


def _run(*args):
    r = subprocess.run([sys.executable, TOOL, *args], capture_output=True,
                       text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert r.returncode == 0, r.stderr
    return r.stdout


def _png_dir(tmp_path, frames):
    d = tmp_path / "frames"
    d.mkdir()
    for i, f in enumerate(frames):
        arr = np.frombuffer(b"".join(f), dtype=np.uint8).reshape(240, 320, 3)
        Image.fromarray(arr).save(d / f"{i:03d}.png")
    return d


def _tool_clip(tmp_path, frames, fps, hold):
    out = tmp_path / "t.bin"
    _run("--frames", str(_png_dir(tmp_path, frames)), "--fps", str(fps),
         "--hold", str(hold), "--out", str(out))
    return out.read_bytes()


def _mine_clip(frames, fps, hold_ms):
    shown = le.quantise(frames, 10)
    bg = tuple(frames[0][0][:3])
    return le.build([le.to_rgb565(f) for f in shown], fps, hold_ms, le._bg565(bg))


def test_a_still(tmp_path):
    base = lf.still((0, 0, 0))[0]
    png = tmp_path / "s.png"
    arr = np.frombuffer(b"".join(base), dtype=np.uint8).reshape(240, 320, 3)
    Image.fromarray(arr).save(png)
    out = tmp_path / "s.bin"
    _run("--image", str(png), "--out", str(out))
    rgba = bytearray()
    for row in base:
        for i in range(0, 960, 3):
            rgba += row[i:i + 3] + b"\xff"
    assert out.read_bytes() == le.from_rgba(bytes(rgba), (0, 0, 0), 3000)


def test_a_12_frame_clip_with_motion_in_two_bands(tmp_path):
    frames = lf.clip(12)
    assert _tool_clip(tmp_path, frames, 10, 0.5) == _mine_clip(frames, 10, 500)


def test_a_clip_with_a_repeated_frame(tmp_path):
    frames = lf.with_repeat(6)
    assert _tool_clip(tmp_path, frames, 12, 0.5) == _mine_clip(frames, 12, 500)


def test_the_gate_pair(tmp_path):
    frames = [lf.solid(c) for c in ((0, 0, 0), (10, 0, 0), (11, 0, 0))]
    mine = _mine_clip(frames, 5, 500)
    assert _tool_clip(tmp_path, frames, 5, 0.5) == mine
    assert le.parse(mine)["rects"] == [1, 0, 1]      # gated out, then moved


def test_a_gif_through_the_tool_and_from_gif(tmp_path):
    pal = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255)]
    fs = []
    for i, cs in enumerate((10, 20, 10, 40)):
        idx = bytearray(320 * 240)
        for y in range(40, 80):
            idx[y * 320 + 20 * i:y * 320 + 20 * i + 30] = bytes([1 + i % 3]) * 30
        fs.append(gg.frame(320, 240, bytes(idx), delay_cs=cs))
    data = gg.build(320, 240, pal, fs)
    gif = tmp_path / "a.gif"
    gif.write_bytes(data)
    out = tmp_path / "a.bin"
    _run("--image", str(gif), "--hold", "3", "--out", str(out))
    want = le.from_gif(data, (0, 0, 0), 3000, "fill")
    assert out.read_bytes() == want
    info = le.parse(want)
    text = _run("--info", str(out))
    assert f"{info['nframes']} frame(s) @ {info['fps']} fps, hold 3000 ms" in text
    assert f"max {info['max_rects']} rect(s)" in text
