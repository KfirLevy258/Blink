"""tests/ci/check_codex_contract.sh: a field's serde attributes are read
whole, wherever in the attribute a rename or a skip is written.

Checks 4 and 12 used `grep 'serde(rename\\b\\|serde(rename ='`, which caught
a rename only when it came first in its attribute and caught no skip at
all. Upstream already writes `#[serde(default, ...)]` first on TokenUsage,
so `#[serde(default, rename = "completion_tokens")]` on output_tokens passed
the contract while the burn view drew twenty empty bars (3rd review,
2026-11-01). The helpers are lifted out of the script itself, so this tests
what CI runs. The Rust below is written here, never fetched; the proof
against upstream's own protocol.rs, mutated, was run offline with
CODEX_SRC_DIR (see the commit).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "ci" / "check_codex_contract.sh"
LIB = ROOT / "tests" / "ci" / "lib_rust.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None, reason="needs sh")

# Source lib_rust.sh, then define the two helpers exactly as the script has
# them, each from its header to the first line that is a lone "}".
RUN = r'''
. "$0"
eval "$(sed -n '/^field_attrs()/,/^}$/p' "$1")"
eval "$(sed -n '/^serialised_elsewhere()/,/^}$/p' "$1")"
if serialised_elsewhere "$(field_attrs "$2" "$3" "$4")"; then
	echo moved
else
	echo kept
fi
'''


def verdict(tmp_path, text, item, field):
    src = tmp_path / "protocol.rs"
    src.write_text(text, encoding="utf-8")
    r = subprocess.run(["sh", "-c", RUN, str(LIB), str(SCRIPT), str(src),
                        item, field],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def usage(attr):
    return ("#[derive(Serialize)]\npub struct TokenUsage {\n"
            "    #[serde(default)]\n"
            "    #[ts(type = \"number\")]\n"
            "    pub cache_write_input_tokens: i64,\n"
            "    /// The output.\n"
            f"{attr}"
            "    #[ts(type = \"number\")]\n"
            "    pub output_tokens: i64,\n"
            "    #[serde(default, skip_serializing)]\n"
            "    #[ts(skip)]\n"
            "    pub codex_rollout_budget_units: Option<i64>,\n"
            "}\n")


@pytest.mark.parametrize("attr", [
    '    #[serde(rename = "completion_tokens")]\n',
    '    #[serde(default, rename = "completion_tokens")]\n',
    '    #[serde(rename(serialize = "completion_tokens"))]\n',
    '    #[serde(skip)]\n',
    '    #[serde(default, skip_serializing)]\n',
    '    #[serde(skip_serializing_if = "is_zero")]\n',
    '    #[serde(\n        default,\n        rename = "completion_tokens"\n    )]\n',
])
def test_a_rename_or_skip_anywhere_in_the_attribute_moves_the_key(
        tmp_path, attr):
    assert verdict(tmp_path, usage(attr), "pub struct TokenUsage",
                   "pub output_tokens:") == "moved"


@pytest.mark.parametrize("attr", [
    "",
    "    #[serde(default)]\n",
    '    #[serde(default, alias = "completion_tokens")]\n',
    "    #[ts(skip)]\n",
    "    #[schemars(skip)]\n",
])
def test_attributes_that_leave_the_key_alone_pass(tmp_path, attr):
    assert verdict(tmp_path, usage(attr), "pub struct TokenUsage",
                   "pub output_tokens:") == "kept"


def test_a_neighbours_skip_is_not_this_fields(tmp_path):
    """Upstream skips codex_rollout_budget_units, right after the fields
    read here; that is no news about output_tokens or any other."""
    text = usage("")
    for field in ("pub output_tokens:", "pub cache_write_input_tokens:"):
        assert verdict(tmp_path, text, "pub struct TokenUsage",
                       field) == "kept"
    assert verdict(tmp_path, text, "pub struct TokenUsage",
                   "pub codex_rollout_budget_units:") == "moved"


def events(attr):
    return ('#[serde(tag = "type", rename_all = "snake_case")]\n'
            "pub enum EventMsg {\n"
            '    #[serde(rename = "task_started", alias = "turn_started")]\n'
            "    TurnStarted(TurnStartedEvent),\n"
            "\n"
            "    /// Usage update.\n"
            f"{attr}"
            "    TokenCount(TokenCountEvent),\n"
            "}\n")


@pytest.mark.parametrize("attr,want", [
    ("", "kept"),
    ('    #[serde(rename = "thread_token_usage", alias = "token_count")]\n',
     "moved"),
    ("    #[serde(skip)]\n", "moved"),
    ('    #[serde(alias = "token_usage")]\n', "kept"),
])
def test_a_per_variant_rename_on_token_count(tmp_path, attr, want):
    """EventMsg renames task_started per variant already; the variant
    before TokenCount's is not TokenCount's."""
    assert verdict(tmp_path, events(attr), "pub enum EventMsg",
                   "TokenCount(") == want
