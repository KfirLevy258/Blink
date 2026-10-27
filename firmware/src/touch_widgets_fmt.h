#ifndef TOUCH_WIDGETS_FMT_H
#define TOUCH_WIDGETS_FMT_H

/*
 * The widget listing's wire format, as pure functions (CONFIG_BLINK_TOUCH_INJECT;
 * touch_widgets.c walks the screen, this says how each thing it finds is
 * written down). Split out like touch_inject_geom.h so a host test can hold
 * every byte of it without LVGL: tests/touch_widgets/host_test.c drives the
 * worst case through these and checks the line still fits.
 *
 * No includes beyond the C library. The only includers are touch_widgets.c
 * and that host test; the build adds touch_widgets.c only with the option on,
 * so nothing here is in a release image.
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/*
 * Four widgets a line. The board's own inbound budget is 512 (proto.c drops a
 * longer line whole), and the bench reads up to 2048, so the wire would take
 * more -- but every printk on this board goes through the deferred log
 * (CONFIG_LOG_PRINTK with LOG_MODE_DEFERRED, LOG_BUFFER_SIZE 1024, overflow
 * drops the OLDEST message). A reply of half that buffer, landing beside a
 * [mem] line and a ping, is how a protocol line silently vanishes. Four at
 * the worst-case entry below is 370 bytes with the head and tail (369 at
 * protocol v2; every aim at 319,239, the widest), which tests/touch_widgets
 * checks, and touch_inject.c kicks the log thread after
 * each page so the next request never finds the last reply still queued.
 */
#define TW_PER_PAGE 4

/*
 * A label is cut at 28 characters. The longest copy a control carries today
 * is the Edit slot's "Run `blink theme` on your computer" (34); a bench
 * matches by prefix, so 28 still names it uniquely, and the line stays
 * inside the budget above.
 */
#define TW_LABEL_MAX 28

/* The reply buffer. 448 rather than 512 so the check in tests/touch_widgets
 * has room to fail before the inbound budget does. */
#define TW_LINE_MAX 448

/* Five by five aim points inside a widget's visible area, centre first. */
#define TW_PROBE_N 25

enum tw_kind {
	TW_K_OBJ = 0,
	TW_K_BUTTON,
	TW_K_SWITCH,
	TW_K_SLIDER,
	TW_K_LIST,	/* a list item: lv_list_button */
	TW_K_CHECK,
	TW_K_SCREEN,	/* the active screen itself */
	TW_K_LAYER,	/* a layer, made clickable by someone */
	TW_K_N,
};

static inline const char *tw_kind_name(int k)
{
	static const char *const names[TW_K_N] = {
		"obj", "button", "switch", "slider", "list", "check",
		"screen", "layer",
	};

	return k >= 0 && k < TW_K_N ? names[k] : "obj";
}

/* The flags word on each entry. */
#define TW_F_CHECKED	0x01
#define TW_F_DISABLED	0x02
#define TW_F_HIT	0x04	/* the real hit test reaches it at (ax, ay) */
#define TW_F_EVENTS	0x08	/* it has an event callback of its own */
#define TW_F_TOP	0x10	/* on lv_layer_top */
#define TW_F_SYS	0x20	/* on lv_layer_sys (or the bottom layer) */
#define TW_F_ALL	0x3f

/*
 * A label as it may go on the wire: printable ASCII, nothing to escape.
 *
 * `"` becomes `'` and `\` becomes `/`, so the JSON string needs no escapes
 * and the worst case is the length and not twice it. A newline (the notices
 * wrap their sentences with one) is a space. The three LVGL symbols the
 * panel's controls wear -- the back chevron, the pager chevrons, the
 * update tick -- are three-byte UTF-8 in the private use area; they become
 * `<`, `>` and `*`, so "Back" and the pager can be named. Anything else
 * outside ASCII is `?`, one per character rather than per byte.
 *
 * The text is the control's own, put there by this firmware or by a protocol
 * line (a version, an OTA error); nothing on this panel takes typed input.
 */
static inline void tw_label_copy(char *dst, size_t n, const char *src)
{
	size_t o = 0;
	const unsigned char *s = (const unsigned char *)(src ? src : "");

	if (n == 0) {
		return;
	}
	while (*s && o + 1 < n && o < TW_LABEL_MAX) {
		unsigned char c = *s;
		char out;

		if (c >= 0x80) {
			/* LV_SYMBOL_LEFT EF 81 93, _RIGHT EF 81 94, _OK EF 80 8C */
			if (c == 0xEF && s[1] == 0x81 && s[2] == 0x93) {
				out = '<';
			} else if (c == 0xEF && s[1] == 0x81 && s[2] == 0x94) {
				out = '>';
			} else if (c == 0xEF && s[1] == 0x80 && s[2] == 0x8C) {
				out = '*';
			} else {
				out = '?';
			}
			/* Past the lead byte and every continuation byte. */
			s++;
			while ((*s & 0xC0) == 0x80) {
				s++;
			}
		} else {
			out = c == '"' ? '\'' : c == '\\' ? '/' :
			      (c == '\n' || c == '\t') ? ' ' :
			      (c < 0x20 || c == 0x7F) ? '?' : (char)c;
			s++;
		}
		dst[o++] = out;
	}
	dst[o] = '\0';
}

/* FNV-1a, 32 bits: the listing's generation, over what was listed. */
#define TW_FNV_INIT 2166136261u

static inline uint32_t tw_fnv(uint32_t h, const void *p, size_t len)
{
	const unsigned char *b = (const unsigned char *)p;

	for (size_t i = 0; i < len; i++) {
		h ^= b[i];
		h *= 16777619u;
	}
	return h;
}

static inline uint32_t tw_fnv_int(uint32_t h, int32_t v)
{
	return tw_fnv(h, &v, sizeof(v));
}

/*
 * Aim point k of TW_PROBE_N inside x1..x2, y1..y2 (inclusive). 0 is the
 * centre, the way a finger aims; the rest are a 5 x 5 grid at the middle of
 * each fifth, tried only when the centre is covered. The grid is what finds
 * the 10 px strip of edge zone a notice leaves down each side of the gauges
 * (ui_settings.c, zone_was_a_tap): the centre of a 44 px zone is under the
 * notice, the fifth column is not.
 */
static inline void tw_probe(int k, int x1, int y1, int x2, int y2,
			    int *px, int *py)
{
	int w = x2 - x1 + 1;
	int h = y2 - y1 + 1;

	if (k <= 0) {
		*px = x1 + (w - 1) / 2;
		*py = y1 + (h - 1) / 2;
		return;
	}
	k -= 1;
	k %= TW_PROBE_N;
	*px = x1 + ((k % 5) * 2 + 1) * w / 10;
	*py = y1 + ((k / 5) * 2 + 1) * h / 10;
}

/*
 * One entry, appended at buf + *at without overrunning `len`:
 *   [i,"kind","label",x,y,w,h,ax,ay,f]
 * comma-led after the first. False, with *at unchanged, when it does not fit.
 */
static inline bool tw_entry_put(char *buf, size_t len, size_t *at, bool first,
				int i, int kind, const char *label, int x,
				int y, int w, int h, int ax, int ay, int flags)
{
	int n;

	if (*at >= len) {
		return false;
	}
	n = snprintf(buf + *at, len - *at,
		     "%s[%d,\"%s\",\"%s\",%d,%d,%d,%d,%d,%d,%d]",
		     first ? "" : ",", i, tw_kind_name(kind), label, x, y, w,
		     h, ax, ay, flags);
	if (n < 0 || (size_t)n >= len - *at) {
		buf[*at] = '\0';
		return false;
	}
	*at += (size_t)n;
	return true;
}

/* The line's head, up to the opening of the entries. */
static inline bool tw_head_put(char *buf, size_t len, size_t *at, int proto_v,
			       int page)
{
	int n = snprintf(buf, len, "{\"t\":\"widgets\",\"v\":%d,\"page\":%d,"
			 "\"w\":[", proto_v, page);

	if (n < 0 || (size_t)n >= len) {
		return false;
	}
	*at = (size_t)n;
	return true;
}

/* And its tail: the count, whether there is a next page, the generation. The
 * generation goes last because it is only known once the walk has seen
 * everything, and the entries are written as the walk passes them. */
static inline bool tw_tail_put(char *buf, size_t len, size_t *at, int n_total,
			       bool more, uint32_t gen)
{
	int n;

	if (*at >= len) {
		return false;
	}
	n = snprintf(buf + *at, len - *at,
		     "],\"n\":%d,\"more\":%d,\"gen\":%lu}", n_total,
		     more ? 1 : 0, (unsigned long)gen);
	if (n < 0 || (size_t)n >= len - *at) {
		return false;
	}
	*at += (size_t)n;
	return true;
}

#endif /* TOUCH_WIDGETS_FMT_H */
