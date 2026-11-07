"""tests/ci/lib_rust.sh: check_codex_contract.sh reads a whole Rust item,
not a window of lines after its header.

The check of SessionMeta used `grep -A20`, and reported "Codex sessions
cannot be named" the day upstream pushed `pub cwd` to line 23 of the struct
(review of develop/fork-picks, 2026-10-28, NICE-1). The Rust below is written
here, never fetched.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "tests" / "ci" / "lib_rust.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None, reason="needs sh")


def _item(tmp_path, text, header):
    src = tmp_path / "protocol.rs"
    src.write_text(text, encoding="utf-8")
    r = subprocess.run(
        ["sh", "-c", '. "$0"; rust_item "$1" "$2"', str(LIB), str(src), header],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _struct(name, fields, tail=""):
    body = "".join(f"    /// field {i}\n    pub {f},\n"
                   for i, f in enumerate(fields))
    return f"#[derive(Serialize)]\npub struct {name} {{\n{body}}}\n{tail}"


def test_a_field_far_down_a_long_struct_is_found(tmp_path):
    fields = [f"f{i}: u32" for i in range(30)] + ["cwd: PathBuf"]
    out = _item(tmp_path, _struct("SessionMeta", fields), "pub struct SessionMeta")
    assert "pub cwd: PathBuf" in out
    assert out.rstrip().endswith("}")


def test_the_item_ends_at_its_closing_brace(tmp_path):
    """A field of the NEXT item is not this one's, however close it is."""
    text = (_struct("SessionMeta", ["id: Uuid"])
            + _struct("Other", ["cwd: PathBuf"]))
    out = _item(tmp_path, text, "pub struct SessionMeta")
    assert "pub id: Uuid" in out
    assert "cwd" not in out


def test_nested_braces_are_counted(tmp_path):
    text = ("impl CodexErrorInfo {\n"
            "    pub fn affects_turn_status(&self) -> bool {\n"
            "        match self {\n"
            "            Self::A { .. } => false,\n"
            "            _ => true,\n"
            "        }\n"
            "    }\n"
            "    pub fn after(&self) -> bool { false }\n"
            "}\n")
    out = _item(tmp_path, text, "pub fn affects_turn_status")
    assert "Self::A { .. } => false," in out
    assert "_ => true," in out
    assert "after" not in out


def test_the_header_is_the_whole_name(tmp_path):
    text = (_struct("SessionMetaLine", ["cwd: PathBuf"])
            + _struct("SessionMeta", ["id: Uuid"]))
    out = _item(tmp_path, text, "pub struct SessionMeta")
    assert "SessionMetaLine" not in out
    assert "pub id: Uuid" in out and "cwd" not in out


@pytest.mark.parametrize("decl", [
    "pub struct SessionMeta(pub Inner);",
    "pub struct SessionMeta;",
])
def test_an_item_with_no_body_ends_at_its_semicolon(tmp_path, decl):
    """Without a brace it used to run to the end of the file, and the next
    item's cwd passed the check (review of fp/fix-tools, NICE 1)."""
    text = decl + "\n" + _struct("Other", ["cwd: PathBuf"])
    out = _item(tmp_path, text, "pub struct SessionMeta")
    assert out.strip() == decl


def test_a_comment_naming_the_header_is_not_the_header(tmp_path):
    text = ("/// replaces pub struct SessionMeta { cwd }\n"
            + _struct("SessionMeta", ["id: Uuid"]))
    out = _item(tmp_path, text, "pub struct SessionMeta")
    assert out.startswith("pub struct SessionMeta {")
    assert "replaces" not in out


def test_no_such_item_prints_nothing(tmp_path):
    assert _item(tmp_path, _struct("Else", ["a: u8"]),
                 "pub struct SessionMeta") == ""


def test_the_contract_check_has_no_fixed_window_left():
    text = (ROOT / "tests" / "ci" / "check_codex_contract.sh").read_text(
        encoding="utf-8")
    code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    # Nor one above an item: the container checks read the item's whole
    # attribute block through rust_attrs (4th review, 2026-11-01).
    assert not [l for l in code if "grep -A" in l or "grep -B" in l]
    # Seven checks, and field_attrs and field_decl, which read each field's
    # attributes and its own line inside its own item for checks 4 and 12
    # (3rd and 4th reviews, 2026-11-01).
    assert sum("rust_item " in l for l in code) == 9
