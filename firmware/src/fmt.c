#include <stdint.h>
#include <stdio.h>
#include "fmt.h"

void fmt_countdown(int32_t secs, char *buf, size_t buflen)
{
	if (buf == NULL || buflen == 0) {
		return;
	}
	if (secs < 0) {
		snprintf(buf, buflen, "--");
		return;
	}
	if (secs == 0) {
		snprintf(buf, buflen, "now");
		return;
	}

	int32_t days = secs / 86400;
	int32_t hours = (secs % 86400) / 3600;
	int32_t mins = (secs % 3600) / 60;
	int32_t rem = secs % 60;

	if (days > 0) {
		snprintf(buf, buflen, "%dd %dh", days, hours);
	} else if (hours > 0) {
		snprintf(buf, buflen, "%dh %02dm", hours, mins);
	} else {
		snprintf(buf, buflen, "%dm %02ds", mins, rem);
	}
}

void fmt_burn(double pph, char *buf, size_t buflen)
{
	if (buf == NULL || buflen == 0) {
		return;
	}
	/*
	 * Empty, not "--". The caller has its own idea of what "nothing to
	 * show" looks like and already draws it; returning a second spelling
	 * of nothing would put two different blanks on one screen.
	 */
	if (!(pph > 0)) {	/* also catches NaN */
		buf[0] = '\0';
		return;
	}
	if (pph < 10.0) {
		snprintf(buf, buflen, "+%.1f%%/h", pph);
	} else if (pph < 1000.0) {
		snprintf(buf, buflen, "+%d%%/h", (int)(pph + 0.5));
	} else {
		/* A rate this high means the window is about to be full
		 * regardless of the exact figure, and four digits would run
		 * past the countdown's width budget. */
		snprintf(buf, buflen, "+999%%/h");
	}
}

void fmt_age(int32_t secs, char *buf, size_t buflen)
{
	if (buf == NULL || buflen == 0) {
		return;
	}
	if (secs < 0) {
		snprintf(buf, buflen, "never");
		return;
	}
	if (secs < 60) {
		snprintf(buf, buflen, "%ds ago", secs);
	} else if (secs < 3600) {
		snprintf(buf, buflen, "%dm ago", secs / 60);
	} else {
		snprintf(buf, buflen, "%dh %dm ago", secs / 3600, (secs % 3600) / 60);
	}
}

/* One UTF-8 sequence -> the ASCII string to draw for it. */
static const char *ascii_for(uint32_t cp)
{
	switch (cp) {
	case 0x2018: case 0x2019: return "'";	/* smart single quotes */
	case 0x201C: case 0x201D: return "\"";	/* smart double quotes */
	case 0x2013: case 0x2014: return "-";	/* en/em dash */
	case 0x2026: return "...";		/* ellipsis */
	case 0x00A0: return " ";		/* no-break space */
	default: return "?";
	}
}

void fmt_ascii(const char *src, char *dst, size_t dstlen)
{
	if (dst == NULL || dstlen == 0) {
		return;
	}
	size_t o = 0;

	while (src && *src && o + 1 < dstlen) {
		unsigned char c = (unsigned char)*src;

		if (c < 0x80) {
			dst[o++] = *src++;
			continue;
		}

		/* Decode one multi-byte sequence (tolerate truncation). */
		int extra = (c >= 0xF0) ? 3 : (c >= 0xE0) ? 2 : 1;
		uint32_t cp = c & (0x3F >> extra);

		src++;
		for (int i = 0; i < extra && (*src & 0xC0) == 0x80; i++) {
			cp = (cp << 6) | (*src++ & 0x3F);
		}

		const char *rep = ascii_for(cp);

		while (*rep && o + 1 < dstlen) {
			dst[o++] = *rep++;
		}
	}
	dst[o] = '\0';
}

void fmt_hint(const char *status, const char *label, char *buf, size_t buflen)
{
	if (!buf || buflen == 0) {
		return;
	}
	buf[0] = '\0';
	if (!status || !status[0]) {
		return;
	}

	if (label && label[0]) {
		char ascii[FMT_HINT_MAX];

		fmt_ascii(label, ascii, sizeof(ascii));
		if (ascii[0]) {
			snprintf(buf, buflen, "%s - %s", status, ascii);
			return;
		}
	}
	snprintf(buf, buflen, "%s", status);
}

/*
 * Four groups fit the 120 px from the bezel to the wordmark.
 *
 * It was three, when the row lived in the 75 px between the clock and the
 * wordmark and the overflow rule below ran on an ordinary desk. The clock
 * moved to the row under the brand -- which is the clock's row now, and only
 * yields to a sentence when something wants a person -- and the corner it left
 * is the row's. There are only four states, so the cap is no longer reachable
 * by a real frame; it stays because `max` can still be smaller than four, and
 * because a fifth state would otherwise silently take a slot from RUNNING.
 */
#define PIP_GROUPS_MAX	4
/* Above this many sessions a row of pips is counted rather than read. */
#define PIP_SESSIONS_MAX 6

int fmt_pips(int n_run, int n_wait, int n_fail, int n_fin,
	     struct fmt_pip *out, int max)
{
	if (!out || max <= 0) {
		return 0;
	}
	if (n_run < 0) { n_run = 0; }
	if (n_wait < 0) { n_wait = 0; }
	if (n_fail < 0) { n_fail = 0; }
	if (n_fin < 0) { n_fin = 0; }

	/* Most urgent first: the eye lands on the left of this row. */
	const enum fmt_pip_kind kinds[4] = {
		FMT_PIP_FAILED, FMT_PIP_WAITING, FMT_PIP_RUNNING, FMT_PIP_FINISHED
	};
	const int counts[4] = { n_fail, n_wait, n_run, n_fin };
	int total = n_fail + n_wait + n_run + n_fin;
	int w = 0;

	if (total == 0) {
		return 0;
	}

	if (total <= PIP_SESSIONS_MAX) {
		for (int k = 0; k < 4 && w < max; k++) {
			for (int j = 0; j < counts[k] && w < max; j++) {
				out[w].kind = kinds[k];
				out[w].count = 0;
				w++;
			}
		}
		return w;
	}

	/*
	 * Counts mode. Walking the array in urgency order and stopping at
	 * PIP_GROUPS_MAX drops FINISHED first and then RUNNING for free --
	 * they are simply last in line, so the rule needs no separate branch
	 * that could disagree with the ordering above.
	 */
	for (int k = 0; k < 4 && w < max && w < PIP_GROUPS_MAX; k++) {
		if (counts[k] == 0) {
			continue;
		}
		out[w].kind = kinds[k];
		out[w].count = counts[k];
		w++;
	}
	return w;
}
