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
