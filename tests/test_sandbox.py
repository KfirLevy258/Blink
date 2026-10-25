"""The test sandbox in tests/conftest.py, and the one hole it cannot close.

conftest.py redirects the home directory for every test, but only for code
that asks where home is AFTER the fixture has run. A path worked out at
import -- a module constant, a default argument, a class attribute -- is
fixed before any fixture exists and names the real home whatever conftest
does. CLAUDE.md names that hole, and it has been fallen into twice: the
status-line payload path (found by gapietro in their fork) and, before it,
ClaudeCliProvider taking that constant as its default (2026-10-19). Both
read this desk's live statusline.json from inside the suite.

So the rule is checked on the source rather than left to review: nothing
evaluated at import, anywhere under pc/ or in the two entry points, may ask
for home -- directly, or through a function of ours that does.
"""
import ast
import os
import pathlib

from pc.providers import codex_cli
from tests import conftest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Exactly the variables the sandbox moves or clears. Reading one at import
# is the same hole as expanduser: the value is the real one.
HOME_VARS = set(conftest.SET_UNDER_HOME) | set(conftest.CLEARED)

# Library calls that resolve a home directory, by the name they are called
# through. expandvars is here because "$HOME/..." is expanduser by another
# spelling.
HOME_CALLS = {"expanduser", "expandvars", "home"}


def _sources():
    files = sorted((ROOT / "pc").rglob("*.py"))
    files += [ROOT / "claude_usage_bridge.py", ROOT / "blink_main.py"]
    return files


def _called_name(call):
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    return getattr(f, "id", "")


def _is_environ(node):
    """`os.environ`, or `environ` brought in by `from os import environ`."""
    return ((isinstance(node, ast.Attribute) and node.attr == "environ")
            or (isinstance(node, ast.Name) and node.id == "environ"))


def _asks_for_home(node, helpers=frozenset()):
    """Does this one node resolve a home directory?

    `helpers` are the names of our own functions that do (see
    _home_helpers): calling one of them at import is the same hole with a
    function call in front of it, which is the shape `X = ss.payload_path()`
    would take.
    """
    if isinstance(node, ast.Call):
        name = _called_name(node)
        if name in HOME_CALLS or name in helpers:
            return True
        # os.getenv("HOME"), os.environ.get("HOME")
        if name in ("getenv", "get") and node.args:
            arg = node.args[0]
            return isinstance(arg, ast.Constant) and arg.value in HOME_VARS
    if isinstance(node, ast.Subscript):
        # os.environ["HOME"]
        key = node.slice
        return (_is_environ(node.value) and isinstance(key, ast.Constant)
                and key.value in HOME_VARS)
    if isinstance(node, ast.ImportFrom):
        # Not a lookup yet, but each turns every later use into a bare name
        # -- under an alias, a name nothing here can recognise -- and
        # nothing in this tree needs them.
        names = {a.name for a in node.names}
        return ((node.module == "os" and "environ" in names)
                or (node.module == "os.path"
                    and bool(names & {"expanduser", "expandvars"})))
    return False


def _is_main_guard(stmt):
    """`if __name__ == "__main__":` -- runs as a program, never on import."""
    t = stmt.test if isinstance(stmt, ast.If) else None
    return (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
            and t.left.id == "__name__" and len(t.comparators) == 1
            and isinstance(t.comparators[0], ast.Constant)
            and t.comparators[0].value == "__main__")


def _import_time_nodes(body):
    """Every node in `body` that runs when the module is imported.

    A function's BODY runs when it is called, so it is skipped; but its
    default values and decorators run at the `def`, which is import, and a
    class body runs at import in full. Those two are exactly where a constant
    in disguise hides. A __main__ block runs only as a program.
    """
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in stmt.decorator_list + stmt.args.defaults + [
                    k for k in stmt.args.kw_defaults if k is not None]:
                yield from ast.walk(d)
        elif isinstance(stmt, ast.ClassDef):
            for d in stmt.decorator_list + stmt.bases:
                yield from ast.walk(d)
            yield from _import_time_nodes(stmt.body)
        elif _is_main_guard(stmt):
            yield from _import_time_nodes(stmt.orelse)
        else:
            stack = [stmt]
            while stack:
                n = stack.pop()
                yield n
                # A lambda called on the spot runs its body now, at import.
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Lambda):
                    stack.append(n.func.body)
                for child in ast.iter_child_nodes(n):
                    if isinstance(child, ast.Lambda):
                        stack.extend(child.args.defaults)
                        stack.extend(d for d in child.args.kw_defaults
                                     if d is not None)
                        continue
                    if isinstance(child, (ast.FunctionDef,
                                          ast.AsyncFunctionDef,
                                          ast.ClassDef)):
                        # A def nested in an `if` at module scope: same rule.
                        yield from _import_time_nodes([child])
                        continue
                    stack.append(child)


def _home_helpers(trees):
    """Names of our module-level functions that resolve home when called.

    Found from the source rather than listed, so a new helper is covered the
    day it is written, and to a fixed point, so blink_home() counts because
    it calls _home(). Methods are left out: their names (`path`, `root`) are
    too common to treat a call to any of them as a call to ours.
    """
    funcs = {}
    for tree in trees:
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs[stmt.name] = stmt
    helpers = set()
    while True:
        found = {name for name, fn in funcs.items() if name not in helpers
                 and any(_asks_for_home(n, helpers) for n in ast.walk(fn))}
        if not found:
            return frozenset(helpers)
        helpers |= found


def offenders(sources):
    """(name, line) for every import-time request for home.

    `sources` is {name: source text}; helpers are worked out across all of
    them together, since a constant in one module may call a helper in
    another.
    """
    trees = {name: ast.parse(src, name) for name, src in sources.items()}
    helpers = _home_helpers(trees.values())
    return [(name, n.lineno) for name, tree in trees.items()
            for n in _import_time_nodes(tree.body)
            if _asks_for_home(n, helpers)]


def _the_program_running(sources):
    """The one import-time call allowed: blink_main.py's last line.

    blink_main.py is the frozen binary's script and ends in
    `raise SystemExit(main())` at module scope. That is the program running
    -- what a __main__ block does by another spelling -- and nothing imports
    the file. Exempted by place, not by the name `main`, so a module-level
    main() anywhere else, or any other line of blink_main.py, is still
    caught.
    """
    src = sources.get("blink_main.py")
    if src is None:
        return set()
    last = ast.parse(src).body[-1]
    assert ast.unparse(last) == "raise SystemExit(main())", (
        "blink_main.py no longer ends where this exemption expects")
    return {("blink_main.py", last.lineno)}


def import_time_offenders(sources):
    """offenders(), less the program-running exemption."""
    allowed = _the_program_running(sources)
    return [o for o in offenders(sources) if o not in allowed]


def test_nothing_resolves_home_at_import():
    sources = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8")
               for p in _sources()}
    found = import_time_offenders(sources)
    assert found == [], (
        "home resolved at import, before tests/conftest.py can redirect it; "
        "resolve it inside a function instead (see CLAUDE.md): %r" % found)


def test_the_guard_catches_every_shape_the_hole_has_taken():
    """The checker itself. The first two are the statusline_source code this
    guard was written after; the helper case is the same constant moved one
    call away."""
    caught = [
        'import os\nPAYLOAD = os.path.expanduser("~/.blink/x")\n',
        'import os\ndef read(path=os.path.expanduser("~/x")):\n    pass\n',
        'import pathlib\nclass P:\n    root = pathlib.Path.home()\n',
        'import os\nHOME = os.environ["HOME"]\n',
        'import os\nC = os.environ.get("CODEX_HOME")\n',
        'import os\nU = os.getenv("USERPROFILE")\n',
        'import os\nif True:\n    H = os.path.expanduser("~")\n',
        'import os\nf = lambda p=os.path.expanduser("~"): p\n',
        'import os\nf = lambda *, p=os.path.expanduser("~"): p\n',
        'import os\ndef main():\n    return os.path.expanduser("~")\n'
        'main()\n',
        'import os\nX = os.path.expandvars("$HOME/x")\n',
        'from os import environ\n',
        'import os\ndef _home():\n    return os.path.expanduser("~")\n'
        'def where():\n    return _home() + "/x"\nW = where()\n',
        'import os\ndef f(*, p=os.path.expanduser("~")):\n    return p\n',
        # A lambda's body is skipped as call-time code -- unless it is called
        # right there, at import.
        'import os\nH = (lambda: os.path.expanduser("~"))()\n',
        # Banned like `from os import environ`: under its own name or an
        # alias, every later call is a bare name the checks above miss.
        'from os.path import expanduser\n',
        'from os.path import expanduser as eu\nH = eu("~")\n',
        'from os.path import expandvars\n',
    ]
    for src in caught:
        assert offenders({"<src>": src}), src


def test_the_program_running_is_exempt_by_place_and_nothing_else():
    """blink_main.py's last line is the one allowed call. A second offending
    line in the same file is still caught."""
    blink_main = ('import os\nfrom pc.cli import main\n'
                  'HOME = os.path.expanduser("~")\n'
                  'raise SystemExit(main())\n')
    helper = ('import os\ndef main():\n'
              '    return os.path.expanduser("~")\n')
    found = import_time_offenders({"blink_main.py": blink_main,
                                   "pc/cli.py": helper})
    assert found == [("blink_main.py", 3)]


def test_the_guard_follows_a_helper_across_modules():
    found = offenders({
        "a.py": 'import os\ndef payload_path():\n'
                '    return os.path.expanduser("~/x")\n',
        "b.py": 'import a\nPATH = a.payload_path()\n',
    })
    assert found == [("b.py", 2)]


def test_the_guard_leaves_call_time_resolution_alone():
    fine = [
        'import os\ndef path():\n    return os.path.expanduser("~/x")\n',
        'import os\nclass P:\n    def root(self):\n'
        '        return os.environ.get("HOME")\n',
        'import os\nf = lambda: os.path.expanduser("~")\n',
        'import os\nX = os.environ.get("BLINK_SKIP_SERVICE")\n',
        'import os\nif __name__ == "__main__":\n'
        '    print(os.path.expanduser("~"))\n',
    ]
    for src in fine:
        assert offenders({"<src>": src}) == [], src


def test_the_sandbox_clears_codex_home_even_when_it_is_exported(
        tmp_path, monkeypatch):
    """CODEX_HOME outranks HOME in codex_cli.sessions_root, so redirecting
    HOME alone left any developer who exports it reading their real Codex
    rollouts from inside the suite.

    Exported HERE, then the sandbox applied: a test that only checked the
    environment it was handed would pass on every CI runner, none of which
    sets the variable, with the clear deleted.
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "real-codex"))
    conftest.sandbox_environment(monkeypatch, tmp_path)

    assert "CODEX_HOME" not in os.environ
    assert pathlib.Path(codex_cli.sessions_root()) == (
        tmp_path / ".codex" / "sessions")


def test_move_home_moves_every_home_variable(tmp_path, move_home):
    """The helper a test uses to move home AFTER import, so it moves the
    same set of variables the sandbox does and no test re-derives that set."""
    elsewhere = tmp_path / "elsewhere"
    move_home(elsewhere)
    assert os.path.expanduser("~") == str(elsewhere)
    for var in ("HOME", "USERPROFILE"):
        assert os.environ[var] == str(elsewhere)
    assert os.environ["APPDATA"] == str(elsewhere / "AppData" / "Roaming")
    assert os.environ["LOCALAPPDATA"] == str(elsewhere / "AppData" / "Local")
