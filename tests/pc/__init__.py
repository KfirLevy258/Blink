"""Refuses to be imported by a script while home is the account's real one.

Every helper in this package was written for pytest, where tests/conftest.py
has already pointed ~ into tmp_path before the helper runs. A script that
imports one gets no conftest. On 2026-10-27 an agent measuring the hello
burst did exactly that -- `from tests.pc import test_hello_burst` and then
`_hello_burst()` from a scratch script -- and the helper's theme.save()
wrote the widest custom theme into the developer's real ~/.blink/theme.json,
where the live daemon took it as the owner's choice and the colours outlived
the run as the board's custom slot.

That helper now takes its directory from the caller, but nothing stops the
next helper from reaching for ~, so the package refuses the shape of the
accident itself: imported outside pytest with HOME still the real home. A
script that points HOME (and USERPROFILE) at a scratch directory first is
let through; that is the pattern that was already safe.

"_pytest" in sys.modules is how pytest is told apart from a script: pytest
imports this package while collecting, before the autouse sandbox has moved
HOME, so a check on HOME alone would refuse the suite itself. It is a guard
against accidents, not against a script that imports pytest to get past it.
"""
import os
import sys


def _home_is_real():
    """Is ~ the home the account database names for this user?

    Asked of pwd rather than of $HOME, because $HOME is the thing being
    checked. Windows has no pwd, and nothing there says which profile is the
    real one, so every script import is refused there; under pytest the
    check is never reached.
    """
    try:
        import pwd
    except ImportError:
        return True
    try:
        real = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:            # uid with no entry, as in some containers
        return True
    here = os.path.expanduser("~")
    return os.path.realpath(here) == os.path.realpath(real)


if "_pytest" not in sys.modules and _home_is_real():
    raise ImportError(
        "tests.pc imported outside pytest with HOME at the real home; its "
        "helpers write under ~ (see tests/pc/__init__.py). Run it under "
        "pytest, or point HOME and USERPROFILE at a scratch directory first.")
