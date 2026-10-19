"""Put Blink's state plugin where OpenCode loads plugins from, and take it away.

OpenCode has no shell-hook config of the kind Claude Code and Codex have (none
in 1.19.3's config schema, checked in packages/core/src/v1/config), so the
shim in tools/blink-hook.sh has nothing to be registered with. What it has
instead is a plugin directory: every *.js / *.ts file in
<config>/opencode/plugin/ (or plugins/) is loaded at startup, where <config>
is $XDG_CONFIG_HOME, else ~/.config -- on Windows too, because OpenCode's path
library (xdg-basedir 5.1.0) has no Windows case. tools/blink-opencode.js is
that plugin: the shim, translated, writing the same files into
~/.blink/state-opencode. See docs/plans/opencode.md.

Unlike the two hook installers this edits nobody's configuration. It writes
one file of its own, <config>/opencode/plugin/blink.js, and nothing else: no
key in opencode.json, because a file in that directory is discovered by
location alone (measured -- the lab's capture plugin was never named in any
config). Which makes the one rule that matters here ownership of a FILENAME:
that directory is documented as the place people put their own plugins, so a
blink.js somebody else wrote is theirs, and nothing here may overwrite, refresh
or delete it. Ours is recognised by its first line, the marker below, which
tools/blink-opencode.js carries and tests/ci/check_opencode_plugin.sh pins.

Not a marker file under ~/.blink, as install_codex_hooks keeps. That file
exists because a hook entry is a command string inside somebody else's JSON,
and the string moves whenever the shim does; this is a whole file whose
contents say whose it is, so the proof travels with the thing it proves, and a
~/.blink deleted first cannot orphan it.

OPENCODE'S OWN SIDE EFFECT, which is not ours and which no code here causes:
for every config directory it reads, OpenCode writes a package.json
({"dependencies":{"@opencode-ai/plugin":"<its version>"}}) and a .gitignore,
and runs `npm install` there in the background -- measured in the lab with
nothing but OpenCode's own defaults present, before any plugin of ours
existed. Somebody who finds node_modules/ beside our blink.js should not be
told we put it there. Our plugin imports node: builtins only, so it loads
whether or not that install ever succeeds.

Every function here returns a sentence rather than raising, on every failure
-- a directory we cannot read, a file we cannot write, a plugin directory that
is a file. `blink install` and `blink uninstall` run this on every machine,
including the many with no OpenCode, and a step that can abort either over a
directory that belongs to another tool is worse than one that reports what it
could not do. install_codex_hooks.uninstall's docstring has the incident.
"""
import os

# The first line of tools/blink-opencode.js begins with this, and only a file
# whose first line does is ours. A prefix rather than the whole line, so that
# the explanation after it can be reworded without every installed copy
# turning into "somebody else's file" overnight; the prefix itself must never
# change, for exactly that reason.
MARKER = "// blink-opencode-plugin:"

# The plugin's name in the repository and in the frozen bundle (it is shipped
# with --add-data in tools/build_binary.sh, beside blink-hook.sh), and the name
# it is installed under. Different on purpose: inside OpenCode's directory it
# only needs to say whose it is.
SOURCE_NAME = "blink-opencode.js"
INSTALLED_NAME = "blink.js"

# Singular first: it is the form OpenCode's own docs use, so it is where
# somebody looking for what Blink installed will look. OpenCode globs both
# ({plugin,plugins}/*.{ts,js}, packages/opencode/src/config/plugin.ts), which
# is why uninstall checks the plural as well -- a copy of ours there, put by
# hand or by a future version of this file, would load twice.
_PLUGIN_DIRS = ("plugin", "plugins")

# How much of an existing file is read to find its first line. The marker is
# on line one by construction; reading the whole of somebody else's file to
# decide it is not ours would be both slow for a large one and none of our
# business.
_HEAD_BYTES = 4096


def plugin_path(config_dir: str) -> str:
    """Where ours goes: <config_dir>/plugin/blink.js.

    config_dir is OpenCode's own config directory -- $XDG_CONFIG_HOME/opencode
    or ~/.config/opencode, as pc.providers.opencode.config_dir() resolves it --
    not the one above it. Taken as a parameter so that nothing here computes a
    home path, at import or otherwise: the tests hand it a temp dir, and the
    resolution lives in one place.
    """
    return os.path.join(config_dir, _PLUGIN_DIRS[0], INSTALLED_NAME)


def _all_paths(config_dir: str):
    return [os.path.join(config_dir, d, INSTALLED_NAME) for d in _PLUGIN_DIRS]


def _first_line(path: str):
    """The file's first line, or None when there is no readable file there.

    Decoded leniently: this only ever asks "does it start with our marker",
    and a file that is not valid UTF-8 cannot, so replacement characters are
    the right answer rather than a UnicodeDecodeError -- which is a ValueError,
    not an OSError, and escaped install_codex_hooks._read_marker as a crash
    until it grew its own arm for it.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_HEAD_BYTES)
    except OSError:
        return None
    return head.split(b"\n", 1)[0].decode("utf-8", "replace")


def is_ours(path: str) -> bool:
    """True only for a readable file whose first line is our marker.

    The CRLF a git checkout with autocrlf may have added is no obstacle: the
    marker is a prefix, so a trailing \\r on the line does not stop it
    matching.
    """
    line = _first_line(path)
    return line is not None and line.startswith(MARKER)


def _source_text(source):
    """The plugin's text, from the text itself or from a path to it.

    The caller holds one or the other -- pc/cli reads the shims through
    _shim_source(), which already returns text, while a test or a developer
    may simply point at tools/blink-opencode.js -- so both are taken, told
    apart by the marker the text must start with either way.

    Returns (text, None) or (None, why).
    """
    if not isinstance(source, str) or not source:
        return None, "no plugin source was given"
    if source.startswith(MARKER):
        return source, None
    try:
        with open(source, encoding="utf-8") as f:
            text = f.read()
    except (OSError, ValueError) as e:
        # ValueError covers UnicodeDecodeError, and also the embedded NUL that
        # open() rejects in a "path" that was really a mangled text.
        return None, f"the plugin source could not be read ({e})"
    if not text.startswith(MARKER):
        # Never install a file uninstall would then refuse to remove: without
        # the marker it would be stranded in somebody's plugin directory for
        # good, loading on every OpenCode start.
        return None, "the plugin source does not start with Blink's marker"
    return text, None


def _write(path: str, text: str) -> None:
    """Temp file plus os.replace, so OpenCode never loads half a plugin.

    OpenCode reads this directory at every start, and two can start at once (a
    TUI and a `serve`); a truncated file would be a syntax error in the user's
    OpenCode, not merely in Blink. The temp name does NOT end in .js or .ts,
    because OpenCode loads every file that does -- a temp left by a killed
    install would otherwise load as a second copy of the plugin.

    newline="\\n" for the reason pc/cli._write_shim gives: Windows would
    otherwise write CRLF, which is harmless to a JS parser but makes the file
    differ from the bundle byte for byte, so is_current() would call every
    Windows install stale and rewrite it forever.

    A symlinked blink.js is written through to its target, as
    install_statusline._save does for a dotfiles-managed settings.json:
    os.replace() replaces the LINK, which would leave the real file orphaned
    in somebody's dotfiles repository.
    """
    if os.path.islink(path):
        path = os.path.realpath(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = "%s.blink-tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_current(config_dir: str, expected_text: str) -> bool:
    """Is our installed plugin byte-identical to the one we would install?

    Byte-identical for the reason pc/cli.shim_is_current gives: the one
    difference that has actually bitten a shim was line endings, and a
    comparison that normalised them would call the broken file healthy.

    False when it is absent, unreadable or not ours: "stale" and "cannot tell"
    have the same response, and the response -- install() -- refuses a file
    that is not ours by itself, so a False here can never become a clobber.
    """
    try:
        with open(plugin_path(config_dir), "rb") as f:
            installed = f.read()
    except OSError:
        return False
    if not isinstance(expected_text, str):
        return False
    return installed == expected_text.encode("utf-8")


def install(config_dir: str, source) -> str:
    """Write <config_dir>/plugin/blink.js. Idempotent. Never raises.

    `source` is the plugin's text or a path to tools/blink-opencode.js.

    Refuses, and changes nothing, when a blink.js is already there and is not
    ours: the directory is OpenCode's and the name is an ordinary one, so a
    file under it that lacks our marker belongs to somebody, and an activity
    light is not worth their plugin. The refusal says what to do about it.
    """
    text, why = _source_text(source)
    if text is None:
        return f"OpenCode plugin not installed: {why}."
    target = plugin_path(config_dir)
    if os.path.lexists(target):
        if os.path.isdir(target):
            return (f"OpenCode plugin not installed: {target} is a directory;"
                    " left it alone.")
        if not is_ours(target):
            return (f"OpenCode plugin not installed: {target} already exists"
                    " and is not Blink's; left it alone. Rename it and run"
                    " install again to add Blink's.")
        if is_current(config_dir, text):
            return "OpenCode plugin already installed."
        verb = "updated"
    else:
        verb = "installed"
    try:
        _write(target, text)
    except OSError as e:
        return f"OpenCode plugin could not be written ({e}); nothing changed."
    return f"OpenCode plugin {verb} ({target})."


def refresh(config_dir: str, source):
    """Rewrite our plugin if it is installed and stale; None when nothing to do.

    For `blink update` and the daemon's drift repair, which must put this
    version's plugin on disk -- the plugin ships inside the program, so an
    update leaves the previous release's copy behind exactly as it leaves the
    previous shim -- but must not INSTALL one where there was none: a path
    that does not exist belongs to an install that never happened or was
    undone, and `update` is not the command that sets one up. Same rule as
    pc/cli._refresh_shims.
    """
    target = plugin_path(config_dir)
    try:
        if not os.path.lexists(target) or not is_ours(target):
            return None
    except (OSError, ValueError):
        return None
    text, _ = _source_text(source)
    if text is None or is_current(config_dir, text):
        return None
    return install(config_dir, text)


def uninstall(config_dir: str) -> str:
    """Remove our plugin, and only ours. Never raises.

    A blink.js without our marker is left exactly where it is, whoever wrote
    it. The directory is left too, even when we were the last file in it:
    OpenCode creates and populates that tree itself (see OPENCODE'S OWN SIDE
    EFFECT above), so we cannot prove the directory is ours to remove.
    """
    removed = []
    refused = []
    failed = []
    for path in _all_paths(config_dir):
        if not os.path.lexists(path):
            continue
        if os.path.isdir(path) or not is_ours(path):
            refused.append(path)
            continue
        try:
            os.remove(path)
        except OSError as e:
            failed.append(f"{path} ({e})")
            continue
        removed.append(path)
    parts = []
    if removed:
        parts.append("OpenCode plugin removed (%s)." % ", ".join(removed))
    if failed:
        parts.append("OpenCode plugin could not be removed: %s; left it."
                     % "; ".join(failed))
    if refused:
        parts.append("%s not Blink's; left it alone." % ", ".join(
            p + " is" for p in refused))
    if not parts:
        return "No OpenCode plugin to remove."
    return " ".join(parts)


def announce(config_dir: str) -> str:
    """What installing will put on disk, said before it happens.

    Install is unattended, so this is the only thing standing between us and
    configuring a third vendor's tool without saying so -- the reasoning in
    pc/cli._announce_codex_hooks, which this matches line for line in shape.
    Returned rather than printed, so the caller decides where it goes and a
    test can read it.

    Every line here is a promise the plugin keeps in code: the list of what is
    recorded is the list in tools/blink-opencode.js's header, and the
    directory is the only one it writes to. Change one, change the other.

    Wrapped continuation lines start lower case on purpose, as there: a line
    that continues a sentence is not a new one.
    """
    return "\n".join([
        "Blink is about to add a plugin to OpenCode as well.",
        "",
        f"  File     {plugin_path(config_dir)}",
        "  Why      OpenCode keeps whether a session is working, waiting on",
        "           you or rate limited in memory only, so without this the",
        "           panel cannot show it.",
        "  Records  Event names, session ids, project folder names and",
        "           OpenCode's process id, into ~/.blink/state-opencode.",
        "           Nothing of your prompts, code, tool output or answers.",
        "  Undo     blink uninstall removes it, or delete that one file.",
        "",
    ])
