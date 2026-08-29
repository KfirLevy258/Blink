"""Entry point for the packaged binary.

A one-line launcher so PyInstaller has a script to freeze; the CLI itself
lives in pc/cli.py, where it can be imported and tested without building
anything.

One thing the launcher does itself: `blink -m <module> ...` runs a bundled
Python module the way `python -m` would. The daemon flashes firmware with
esptool and reads eFuses with espefuse, both bundled into this binary, and
invokes them as `sys.executable -m esptool ...` -- which in a frozen build is
THIS program. Without this branch that command was an argparse error, and
every over-the-air firmware update from an installed daemon failed with
"could not read the chip's eFuses" (found on the first real customer-path
test, 2026-08-29; the flow had only ever run from source before).
"""
import sys

if len(sys.argv) > 2 and sys.argv[1] == "-m":
    import runpy

    module = sys.argv[2]
    sys.argv = [module] + sys.argv[3:]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    raise SystemExit(0)

from pc.cli import main  # noqa: E402

raise SystemExit(main())
