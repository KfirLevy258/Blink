/*
 * The widget listing's wire format (firmware/src/touch_widgets_fmt.h): that
 * the worst line it can write fits, that a label goes on the wire with
 * nothing to escape, and that the aim points find the strip of a control a
 * popup leaves uncovered.
 *
 *   cc -I firmware/src -o /tmp/t tests/touch_widgets/host_test.c && /tmp/t
 *
 * (tests/ci/check_host_tests.sh runs this; the line above is for editing it.)
 *
 * The walk itself needs LVGL and is proved on the host harness instead
 * (tools/panel_render/touch.sh with tools/bench/popups_touch.txt), where
 * every listing it makes is parsed by tools/board_touch.py. What is held
 * here is the part a board would get wrong silently: a line over the log
 * buffer's comfort or the 512 the PC's own parser is tested against, or a
 * label that breaks the JSON around it, and the PC would see nothing at all.
 */
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "touch_widgets_fmt.h"

static int fails;

static void eq(const char *what, long got, long want)
{
	if (got != want) {
		fails++;
		printf("FAIL: %s (got %ld, want %ld)\n", what, got, want);
	} else {
		printf("PASS: %s\n", what);
	}
}

static void ok(const char *what, bool cond)
{
	eq(what, cond ? 1 : 0, 1);
}

static void seq(const char *what, const char *got, const char *want)
{
	if (strcmp(got, want) != 0) {
		fails++;
		printf("FAIL: %s (got \"%s\", want \"%s\")\n", what, got, want);
	} else {
		printf("PASS: %s\n", what);
	}
}

/* The longest page there can be: four entries, every number at its widest,
 * the longest kind name, a label at TW_LABEL_MAX, and the tail at its
 * widest generation and count. */
static void worst_line(void)
{
	char buf[TW_LINE_MAX];
	char label[TW_LABEL_MAX + 1];
	size_t at = 0;
	/* A two-digit protocol version, and a HIT aim at the far corner: a
	 * miss's -1,-1 is three characters shorter per entry than 319,239,
	 * which is what this test pinned the first time (review, S1). */
	bool fit = tw_head_put(buf, sizeof(buf), &at, 99, 999);

	memset(label, 'W', TW_LABEL_MAX);
	label[TW_LABEL_MAX] = '\0';
	for (int k = 0; k < TW_PER_PAGE && fit; k++) {
		fit = tw_entry_put(buf, sizeof(buf), &at, k == 0, 9999 - k,
				   TW_K_SCREEN, label, 319, 239, 320, 240,
				   319, 239, TW_F_ALL);
	}
	fit = fit && tw_tail_put(buf, sizeof(buf), &at, 9999, true,
				 4294967295u);
	ok("a worst-case page fits the buffer", fit);
	printf("      worst-case page: %zu bytes of %d\n", at, TW_LINE_MAX);
	/* The figure touch_widgets_fmt.h's comment gives. */
	eq("the worst-case page is the length the header says", (long)at, 370);
	ok("and is under the 512 a protocol line is held to", at < 512);
	ok("it is one JSON object", buf[0] == '{' && buf[at - 1] == '}');
}

static void label_copy(void)
{
	char out[TW_LABEL_MAX + 1];
	char big[100];

	tw_label_copy(out, sizeof(out), "Update now");
	seq("plain words pass through", out, "Update now");

	tw_label_copy(out, sizeof(out), "say \"hi\" a\\b");
	seq("quote and backslash need no escape", out, "say 'hi' a/b");

	tw_label_copy(out, sizeof(out), "Couldn't check\nfor updates.");
	seq("a newline is a space", out, "Couldn't check for updates.");

	tw_label_copy(out, sizeof(out), "\xEF\x81\x93");
	seq("LV_SYMBOL_LEFT is <", out, "<");
	tw_label_copy(out, sizeof(out), "1/3 \xEF\x81\x94");
	seq("LV_SYMBOL_RIGHT is >", out, "1/3 >");
	tw_label_copy(out, sizeof(out), "\xEF\x80\x8C ok");
	seq("LV_SYMBOL_OK is *", out, "* ok");

	tw_label_copy(out, sizeof(out), "caf\xC3\xA9!");
	seq("other UTF-8 is one ? a character", out, "caf?!");

	tw_label_copy(out, sizeof(out), "a\x01" "b\x7f" "c");
	seq("control bytes are ?", out, "a?b?c");

	memset(big, 'x', sizeof(big) - 1);
	big[sizeof(big) - 1] = '\0';
	tw_label_copy(out, sizeof(out), big);
	eq("a long label is cut at TW_LABEL_MAX", (long)strlen(out),
	   TW_LABEL_MAX);

	tw_label_copy(out, sizeof(out), "On your computer, run: blink theme");
	seq("the longest copy on the panel keeps a unique prefix", out,
	    "On your computer, run: blink");

	tw_label_copy(out, sizeof(out), NULL);
	seq("no label is empty", out, "");

	/* Every byte of every output is printable ASCII, not " or \. */
	{
		bool clean = true;

		for (int c = 1; c < 256; c++) {
			char in[2] = { (char)c, 0 };

			tw_label_copy(out, sizeof(out), in);
			for (char *p = out; *p; p++) {
				unsigned char u = (unsigned char)*p;

				if (u < 0x20 || u > 0x7e || u == '"' ||
				    u == '\\') {
					clean = false;
				}
			}
		}
		ok("no single byte yields anything that needs escaping", clean);
	}
}

static void probes(void)
{
	int x, y;
	bool inside = true, strip = false;

	tw_probe(0, 60, 196, 259, 239, &x, &y);
	eq("probe 0 is the centre (x)", x, 159);
	eq("probe 0 is the centre (y)", y, 217);

	for (int k = 0; k < TW_PROBE_N; k++) {
		tw_probe(k, 276, 45, 319, 194, &x, &y);
		if (x < 276 || x > 319 || y < 45 || y > 194) {
			inside = false;
		}
		/* A centred 300 px notice covers x 10..309. */
		if (x >= 310) {
			strip = true;
		}
	}
	ok("every probe is inside the widget", inside);
	ok("the grid reaches the edge zone's strip beside a notice", strip);

	/* A one-pixel widget: every probe is that pixel. */
	inside = true;
	for (int k = 0; k < TW_PROBE_N; k++) {
		tw_probe(k, 159, 40, 159, 40, &x, &y);
		if (x != 159 || y != 40) {
			inside = false;
		}
	}
	ok("a one-pixel widget is probed on its pixel", inside);
}

static void entries(void)
{
	char buf[64];
	size_t at = 0;
	bool put;

	put = tw_entry_put(buf, sizeof(buf), &at, true, 3, TW_K_BUTTON,
			   "OK", 100, 120, 120, 36, 159, 137, TW_F_HIT);
	ok("an entry is written", put);
	seq("in the shape the PC parses", buf,
	    "[3,\"button\",\"OK\",100,120,120,36,159,137,4]");
	put = tw_entry_put(buf, sizeof(buf), &at, false, 4, TW_K_OBJ,
			   "Couldn't check for updates.", 0, 0, 320, 240,
			   -1, -1, 0);
	ok("one that does not fit is refused", !put);
	eq("and leaves the line where it was", (long)at,
	   (long)strlen("[3,\"button\",\"OK\",100,120,120,36,159,137,4]"));
	eq("terminated there", (long)strlen(buf), (long)at);

	seq("an unknown kind is obj", tw_kind_name(99), "obj");
	seq("the kinds board_touch.py knows", tw_kind_name(TW_K_LIST), "list");
}

static void hashes(void)
{
	uint32_t a = tw_fnv(TW_FNV_INIT, "Later", 5);
	uint32_t b = tw_fnv(TW_FNV_INIT, "Later", 5);
	uint32_t c = tw_fnv(TW_FNV_INIT, "Lates", 5);

	eq("the generation is a function of what was listed", a == b, 1);
	eq("one byte different is another generation", a != c, 1);
	eq("an index is hashed by value", tw_fnv_int(TW_FNV_INIT, 7) ==
	   tw_fnv_int(TW_FNV_INIT, 7), 1);
	eq("FNV-1a's offset basis", (long)TW_FNV_INIT, 2166136261L);
}

int main(void)
{
	worst_line();
	label_copy();
	probes();
	entries();
	hashes();
	if (fails) {
		printf("%d check(s) FAILED\n", fails);
		return 1;
	}
	return 0;
}
