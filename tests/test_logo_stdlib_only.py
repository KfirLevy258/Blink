"""The daemon's logo path imports and runs with NumPy and Pillow refused:
pc/ is standard library only (CLAUDE.md), and the encoders it replaces in
tools/ are NumPy throughout."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CHILD = r"""
import sys
sys.modules["numpy"] = None
sys.modules["PIL"] = None
from pc import gif_decode, logo_encode, logo_stream
from tests.support import gen_gif
pal = [(0, 0, 0), (255, 0, 0)]
fs = [gen_gif.frame(16, 12, bytes([i % 2]) * 192, delay_cs=10) for i in range(3)]
img = logo_encode.from_gif(gen_gif.build(16, 12, pal, fs), (0, 0, 0), 3000, "fill")
assert logo_encode.parse(img)["nframes"] == 3
logo_stream.LogoStream(img, "usb")
print("ok")
"""


def test_the_logo_modules_need_neither_numpy_nor_pillow():
    r = subprocess.run([sys.executable, "-c", _CHILD], cwd=ROOT,
                       capture_output=True, text=True,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr
