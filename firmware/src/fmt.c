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
