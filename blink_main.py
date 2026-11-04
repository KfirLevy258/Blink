"""Entry point for the packaged binary.

A one-line launcher so PyInstaller has a script to freeze; the CLI itself
lives in pc/cli.py, where it can be imported and tested without building
anything.

One thing the launcher does itself: `blink -m <module> ...` runs a bundled
Python module the way `python -m` would. The daemon flashes firmware with
esptool and reads the flash-encryption eFuse with pc.efuse_probe, both bundled
into this binary, and invokes them as `sys.executable -m esptool ...` -- which in a frozen build is
THIS program. Without this branch that command was an argparse error, and
every over-the-air firmware update from an installed daemon failed with
"could not read the chip's eFuses" (found on the first real customer-path
test, 2026-08-29; the flow had only ever run from source before).

Nothing here runs on import. This file used to end in a bare
`raise SystemExit(main())`, and a bare `blink` is `blink install`, so on
2026-10-30 (22:25-22:27 IST) a reviewer who imported every module to check
they still loaded on Python 3.9 installed the service: HOME was a scratch
folder, but launchctl acts on the gui/<uid> domain whatever HOME says, so it
booted out the owner's real com.blink.bridge job and bootstrapped a stray
daemon, which ran for a minute against the real ~/.blink and the real board.
PyInstaller runs the entry script as __main__, as does `python blink_main.py`
(the login service, the fleet agent), so the guard costs the launcher nothing.
tests/test_import_is_inert.py imports it in a child with the service tools
stubbed.
"""
import sys


def _run():
    if len(sys.argv) > 2 and sys.argv[1] == "-m":
        import runpy

        module = sys.argv[2]
        sys.argv = [module] + sys.argv[3:]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        raise SystemExit(0)

    from pc.cli import main

    raise SystemExit(main())


if __name__ == "__main__":
    _run()
