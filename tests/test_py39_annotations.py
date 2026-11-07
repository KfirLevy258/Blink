"""No annotation that Python 3.9 evaluates may use 3.10's syntax.

pc/ supports 3.9 (tools/release.sh): /usr/bin/python3 on macOS is 3.9.6,
the interpreter most customers run a source install on, and
.github/workflows/installer.yml runs the suite on it. On feat/usage-refresh
`failing_since: float | None` sat in a @dataclass, whose field annotations
are evaluated when the class is made; on 3.9 that raises TypeError, so
`import pc.usage_refresh` failed and with it pc.cli and the daemon. The
suite here runs on 3.11+, where the same line is fine, so nothing caught it
until a /code-review max imported every module under /usr/bin/python3.

This reads the source instead of needing a 3.9 interpreter: an annotation
that is evaluated at runtime -- a function's parameters and return, a
module or class attribute's -- may not be an `X | Y` union (PEP 604, 3.10)
unless it is a string or the module says `from __future__ import
annotations`. `list[int]` (PEP 585) is 3.9 and fine. Local variable
annotations inside a function are never evaluated and are left alone.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

def _sources():
    yield from sorted((ROOT / "pc").rglob("*.py"))
    for name in ("claude_usage_bridge.py", "blink_main.py"):
        p = ROOT / name
        if p.exists():
            yield p


def _future_annotations(tree):
    return any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
               and any(a.name == "annotations" for a in n.names)
               for n in tree.body)


def _bad(node):
    """The first 3.10-only construct in an annotation expression, or None."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
            return "X | Y"
    return None


def _evaluated(tree):
    """Every annotation 3.9 evaluates at runtime, with its line."""
    def visit(body, in_function):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = n.args
                for arg in (a.posonlyargs + a.args + a.kwonlyargs
                            + [x for x in (a.vararg, a.kwarg) if x]):
                    if arg.annotation is not None:
                        yield arg.annotation
                if n.returns is not None:
                    yield n.returns
                yield from visit(n.body, True)
            elif isinstance(n, ast.ClassDef):
                yield from visit(n.body, False)
            elif isinstance(n, ast.AnnAssign):
                if not in_function:
                    yield n.annotation
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    inner = getattr(n, field, None)
                    if isinstance(inner, list):
                        yield from visit(inner, in_function)
    yield from visit(tree.body, False)


def test_no_runtime_annotation_needs_python_3_10():
    found = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        if _future_annotations(tree):
            continue
        for ann in _evaluated(tree):
            why = _bad(ann)
            if why:
                found.append(f"{path.relative_to(ROOT)}:{ann.lineno} {why}")
    assert not found, ("annotations Python 3.9 evaluates and cannot: quote "
                       "them, as claude_plan does:\n" + "\n".join(found))


def test_the_scan_sees_a_dataclass_field():
    tree = ast.parse("import dataclasses\n"
                     "@dataclasses.dataclass\n"
                     "class S:\n"
                     "    a: float | None = None\n"
                     "    b: 'float | None' = None\n"
                     "def f(x: int | None) -> 'int | None':\n"
                     "    y: int | None = x\n"
                     "    return y\n")
    bad = [ann.lineno for ann in _evaluated(tree) if _bad(ann)]
    assert bad == [4, 6]
