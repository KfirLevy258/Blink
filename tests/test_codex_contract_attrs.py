"""tests/ci/check_codex_contract.sh: a field's serde attributes are read
whole, wherever in the attribute a rename or a skip is written, and an
item's own attributes are read as a block, not as a window of lines.

Checks 4 and 12 used `grep 'serde(rename\\b\\|serde(rename ='`, which caught
a rename only when it came first in its attribute and caught no skip at
all. Upstream already writes `#[serde(default, ...)]` first on TokenUsage,
so `#[serde(default, rename = "completion_tokens")]` on output_tokens passed
the contract while the burn view drew twenty empty bars (3rd review,
2026-11-01). The helpers are lifted out of the script itself, so this tests
what CI runs. The Rust below is written here, never fetched; the proof
against upstream's own protocol.rs, mutated, was run offline with
CODEX_SRC_DIR (see the commit).

The 4th review (2026-11-01) found the 3rd's reader ended an attribute only
at a line ending in `]`: a trailing `// comment` swallowed the field's line
and blamed the next field, a blank line or an attribute on the field's own
line lost it. flatten and an untagged variant moved a key unseen, and a
skip_serializing_if on an Option, harmless to readers that use .get(),
failed the release leg. EventMsg's attributes came from `grep -B4`, so a
rustfmt split failed and an attribute five lines up was not read.
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

# Source lib_rust.sh, then define the helpers exactly as the script has
# them, each from its header to the first line that is a lone "}".
DEFINE = r'''
. "$0"
for fn in field_attrs field_decl item_attrs serialised_elsewhere; do
	eval "$(sed -n "/^$fn()/,/^}\$/p" "$1")"
done
'''
RUN = DEFINE + r'''
if serialised_elsewhere "$(field_attrs "$2" "$3" "$4")" \
		"$(field_decl "$2" "$3" "$4")"; then
	echo moved
else
	echo kept
fi
'''


def _sh(tmp_path, script, text, *args):
    src = tmp_path / "protocol.rs"
    src.write_text(text, encoding="utf-8")
    r = subprocess.run(["sh", "-c", script, str(LIB), str(SCRIPT), str(src),
                        *args],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def verdict(tmp_path, text, item, field):
    return _sh(tmp_path, RUN, text, item, field)


def decl(tmp_path, text, item, field):
    return _sh(tmp_path, DEFINE + 'field_decl "$2" "$3" "$4"\n', text,
               item, field)


def item_attrs(tmp_path, text, item):
    return _sh(tmp_path, DEFINE + 'item_attrs "$2" "$3"\n', text, item)


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


@pytest.mark.parametrize("attr", [
    '    #[serde(rename = "completion_tokens")] // the v2 name\n',
    '    #[serde(rename = "completion_tokens")]\n\n',
    '    #[serde(rename = "completion_tokens")]\n'
    '    /// The output.\n    // v2 spelling\n',
    '    #[serde(\n        default, // older rollouts\n'
    '        rename = "completion_tokens"\n    )]\n',
    '    #[serde(rename = "a]b // c")]\n',
    '    #[serde(flatten)]\n',
    '    #[serde(skip_serializing_if = "is_zero")] // not an Option\n',
])
def test_the_attribute_ends_at_its_own_bracket(tmp_path, attr):
    """A trailing comment, a blank or comment line, a comment inside a split
    attribute, a `]` or `//` inside a string: none of them loses the
    attribute or lets it run on to the field after."""
    assert verdict(tmp_path, usage(attr), "pub struct TokenUsage",
                   "pub output_tokens:") == "moved"


def test_a_trailing_comment_does_not_blame_the_next_field(tmp_path):
    """The 3rd review's reader swallowed output_tokens' line and reported
    reasoning_output_tokens as the renamed one."""
    text = usage('    #[serde(rename = "completion_tokens")] // v2\n')
    text = text.replace("    #[serde(default, skip_serializing)]\n",
                        "    pub reasoning_output_tokens: i64,\n"
                        "    #[serde(default, skip_serializing)]\n")
    assert verdict(tmp_path, text, "pub struct TokenUsage",
                   "pub reasoning_output_tokens:") == "kept"
    assert verdict(tmp_path, text, "pub struct TokenUsage",
                   "pub output_tokens:") == "moved"


def test_an_attribute_on_the_fields_own_line(tmp_path):
    text = usage("").replace(
        "    pub output_tokens: i64,",
        '    #[serde(default)] #[serde(rename = "completion_tokens")] '
        "pub output_tokens: i64,")
    assert verdict(tmp_path, text, "pub struct TokenUsage",
                   "pub output_tokens:") == "moved"
    assert decl(tmp_path, text, "pub struct TokenUsage",
                "pub output_tokens:") == "pub output_tokens: i64,"


def test_an_unclosed_attribute_never_swallows_the_field(tmp_path):
    text = usage('    #[serde(rename = "completion_tokens"\n')
    assert decl(tmp_path, text, "pub struct TokenUsage",
                "pub output_tokens:") == "pub output_tokens: i64,"


def snapshot(attr, ty):
    return ("#[derive(Serialize)]\npub struct RateLimitWindow {\n"
            "    /// Percentage.\n"
            f"{attr}"
            f"    pub field: {ty},\n"
            "}\n")


@pytest.mark.parametrize("ty,want", [
    ("Option<i64>", "kept"),
    ("Option<RateLimitWindow>", "kept"),
    ("std::option::Option<i64>", "kept"),
    ("f64", "moved"),
    ("TokenUsage", "moved"),
])
def test_skip_serializing_if_moves_only_a_non_option(tmp_path, ty, want):
    """On an Option it leaves the key out exactly when it is None, and every
    reader takes these keys with .get(): absent reads as null. On
    used_percent, an f64, a value can vanish."""
    attr = '    #[serde(default, skip_serializing_if = "Option::is_none")]\n'
    assert verdict(tmp_path, snapshot(attr, ty), "pub struct RateLimitWindow",
                   "pub field:") == want


@pytest.mark.parametrize("attr", [
    '    #[serde(rename = "x", skip_serializing_if = "Option::is_none")]\n',
    '    #[serde(skip_serializing_if = "Option::is_none", flatten)]\n',
    "    #[serde(skip)]\n",
])
def test_an_option_is_still_moved_by_rename_skip_or_flatten(tmp_path, attr):
    assert verdict(tmp_path, snapshot(attr, "Option<i64>"),
                   "pub struct RateLimitWindow", "pub field:") == "moved"


def test_an_untagged_variant_moves_the_event(tmp_path):
    assert verdict(tmp_path, events("    #[serde(untagged)] // v2\n"),
                   "pub enum EventMsg", "TokenCount(") == "moved"


def enum(attrs):
    return (f"/// Response event.\n{attrs}"
            "pub enum EventMsg {\n    TokenCount(TokenCountEvent),\n}\n")


DERIVE = "#[derive(Debug, Serialize)]\n"
TS = '#[ts(tag = "type")]\n#[strum(serialize_all = "snake_case")]\n'


def test_a_rustfmt_split_reads_like_one_line(tmp_path):
    one = item_attrs(tmp_path, enum(
        DERIVE + '#[serde(tag = "type", rename_all = "snake_case")]\n' + TS),
        "pub enum EventMsg")
    split = item_attrs(tmp_path, enum(
        DERIVE + '#[serde(\n    tag = "type",\n'
        '    rename_all = "snake_case"\n)]\n' + TS), "pub enum EventMsg")
    assert one == '#[serde(tag = "type", rename_all = "snake_case")]'
    # One line per attribute, its whitespace run together: the split one
    # differs only by the spaces inside its parentheses.
    assert split == '#[serde( tag = "type", rename_all = "snake_case" )]'


def test_an_item_attribute_far_up_is_read(tmp_path):
    """Five attributes above the item: outside the old grep -B4 window."""
    got = item_attrs(tmp_path, enum(
        '#[serde(content = "payload")] // adjacently tagged\n'
        "#[allow(clippy::large_enum_variant)]\n#[non_exhaustive]\n"
        + DERIVE + '#[serde(tag = "type", rename_all = "snake_case")]\n'
        + TS), "pub enum EventMsg")
    assert 'content = "payload"' in got


def test_only_serdes_own_attributes_are_the_items(tmp_path):
    """EventMsg carries #[ts(tag = "type")] too: a changed serde tag must not
    pass on the strength of the ts one."""
    got = item_attrs(tmp_path, enum(
        DERIVE + '#[serde(tag = "kind", rename_all = "snake_case")]\n' + TS),
        "pub enum EventMsg")
    assert got == '#[serde(tag = "kind", rename_all = "snake_case")]'


def test_an_items_name_is_whole(tmp_path):
    text = ('#[serde(rename_all = "camelCase")]\n'
            "pub struct TokenUsageInfo {\n    pub a: i64,\n}\n\n"
            "#[derive(Serialize)]\npub struct TokenUsage {\n"
            "    pub b: i64,\n}\n")
    assert item_attrs(tmp_path, text, "pub struct TokenUsage") == ""
    assert "rename_all" in item_attrs(tmp_path, text,
                                      "pub struct TokenUsageInfo")
