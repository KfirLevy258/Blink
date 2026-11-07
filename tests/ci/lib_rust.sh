# Read one item out of a Rust source file. Sourced, not run, by
# check_codex_contract.sh.
#
#     rust_item FILE 'pub struct SessionMeta'    # prints the whole item
#
# The contract checks used to look inside an item with `grep -A20`, a window
# of twenty lines after its header. Upstream added fields to SessionMeta,
# `pub cwd: PathBuf` moved to line 23, and the check reported that Codex
# sessions could no longer be named when nothing had changed but the length
# of the struct (review of develop/fork-picks, 2026-10-28, NICE-1). Any
# fixed window has that failure in it: widened, it just waits for the next
# field, and it can also run on into the item after it and find a match
# there that this one no longer has.
#
# So this prints from the header to the brace that closes it, counting
# braces, and nothing past that. The header has to be the whole name: a
# following letter, digit or underscore is another item ("SessionMetaLine"
# is not "SessionMeta"), and a line whose text starts with // is a comment
# that mentions the header, not the header.
#
# An item with no body -- `pub struct SessionMeta(pub Inner);`, a unit
# struct -- ends at its `;` if no brace came first. Without that it ran on
# to the end of the file, and a LATER item's `pub cwd: PathBuf` passed the
# check (review of fp/fix-tools, NICE 1). A brace inside a string or a
# comment would still be counted; the items read here are plain
# declarations that have none. No such header prints nothing, which every
# caller already treats as a failure.
rust_item() {
	awk -v hdr="$2" '
	!started {
		if ($0 ~ /^[ \t]*\/\//) next
		i = index($0, hdr)
		if (i == 0) next
		rest = substr($0, i + length(hdr), 1)
		if (rest ~ /[A-Za-z0-9_]/) next
		started = 1
	}
	{
		print
		line = $0
		opens = gsub(/\{/, "{", line)
		closes = gsub(/\}/, "}", line)
		depth += opens - closes
		if (opens > 0) seen = 1
		if (seen && depth <= 0) exit
		if (!seen && index($0, ";") > 0) exit
	}' "$1"
}

# rust_attrs NAME [decl] < SOURCE -> the attributes written on the first
# declaration in SOURCE whose own text starts with NAME, one attribute per
# line with its whitespace run together. With `decl`, the declaration's
# line instead (from NAME on). Prints nothing when there is no such
# declaration, or it has no attribute.
#
#     rust_item FILE 'pub struct TokenUsage' | rust_attrs 'pub output_tokens:'
#     rust_attrs 'pub enum EventMsg' < FILE      # the enum's own attributes
#
# NAME is whole, as rust_item's header is: when it ends in a letter, digit
# or underscore, the next character must not be one, so 'pub struct
# TokenUsage' is not 'pub struct TokenUsageInfo'.
#
# An attribute ends at the `]` that balances its `#[`, counted character by
# character outside string literals, not at a line that happens to end in
# `]`. The line-end version (3rd review) was beaten three ways by the 4th,
# 2026-11-01, each passing a rename that the earlier grep -B2 caught:
# `#[serde(rename = "usage")] // the v2 name` kept it inside the attribute
# until the NEXT field's line, so the field's own line was swallowed and a
# renamed output_tokens was reported as reasoning_output_tokens; a blank
# line between attribute and field threw the attribute away; and an
# attribute on the field's own line, `#[serde(rename = "x")] pub info: ...`,
# was not seen at all. Here a `//` comment (outside a string) ends what is
# read of its line, blank and comment lines keep the block, the text after
# a closing `]` is read as more attributes or as the declaration, and any
# other line starts the block again. A line that starts with NAME is the
# declaration even inside an attribute that never closed, so the field is
# never swallowed by a malformed one above it.
#
# The same reading serves an item's own attributes, so a rustfmt split,
#     #[serde(
#         tag = "type",
#         rename_all = "snake_case"
#     )]
# reads exactly like the one-line form. The checks before this one took a
# fixed `grep -B4` window above the item and kept the lines with `serde(` in
# them: a split attribute lost its continuation lines (EventMsg was then
# "no longer snake_case" for a reformat), and an attribute five lines up was
# not read at all (4th review, 2026-11-01).
rust_attrs() {
	awk -v name="$1" -v mode="${2:-attrs}" '
	function ident(c) { return c ~ /^[A-Za-z0-9_]$/ }
	function is_decl(s,   n) {
		if (index(s, name) != 1) return 0
		n = length(name)
		if (ident(substr(name, n, 1)) && ident(substr(s, n + 1, 1))) return 0
		return 1
	}
	function found(s) {
		if (mode == "decl") print s
		else printf "%s", block
		exit
	}
	# Read s from character pos inside an open attribute; returns the
	# position after its closing ], or 0 when the line ran out first.
	function consume(s, pos,   c, n) {
		n = length(s)
		while (pos <= n) {
			c = substr(s, pos, 1)
			if (instr) {
				if (c == "\\") { cur = cur c; pos++; c = substr(s, pos, 1) }
				else if (c == "\"") instr = 0
			} else if (c == "\"") instr = 1
			else if (c == "/" && substr(s, pos + 1, 1) == "/") return 0
			else if (c == "[") depth++
			else if (c == "]") {
				depth--
				if (depth == 0) {
					cur = cur c
					gsub(/[ \t]+/, " ", cur)
					block = block cur "\n"
					cur = ""
					return pos + 1
				}
			}
			cur = cur c
			pos++
		}
		return 0
	}
	{
		s = $0
		if (depth > 0) {
			t = s; sub(/^[ \t]+/, "", t)
			if (is_decl(t)) { depth = 0; instr = 0; cur = "" }
			else {
				cur = cur " "
				pos = consume(s, 1)
				if (pos == 0) next
				s = substr(s, pos)
			}
		}
		while (1) {
			sub(/^[ \t]+/, "", s)
			if (s == "" || s ~ /^\/\//) next
			if (s ~ /^#\[/) {
				cur = ""; depth = 0; instr = 0
				pos = consume(s, 1)
				if (pos == 0) next
				s = substr(s, pos)
				continue
			}
			if (is_decl(s)) found(s)
			block = ""
			next
		}
	}'
}
