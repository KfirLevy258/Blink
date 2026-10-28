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
