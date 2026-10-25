"""Which program this test process pretends to be: the shipped binary, or a
source checkout.

One pair of helpers rather than a copy per test file. Two copies had already
drifted (one set _MEIPASS and _self_path, the other only sys.frozen), and the
difference decided which shim install read and where it thought it was
running from (review, 2026-10-25).

Neither copies anything. The frozen one points _self_path at installed_bin(),
so install sees itself as already in place and skips the copy, and hands the
bundle's shims over from tools/ the way PyInstaller's _MEIPASS would.
"""
import os
import sys

from pc import cli


def as_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS",
                        os.path.join(cli._repo_root(), "tools"), raising=False)
    monkeypatch.setattr(cli, "_self_path", lambda: cli.installed_bin())


def as_checkout(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
