"""Entry point for the packaged binary.

A one-line launcher so PyInstaller has a script to freeze; the CLI itself
lives in pc/cli.py, where it can be imported and tested without building
anything.
"""
from pc.cli import main

raise SystemExit(main())
