/*
 * SNTP wall-clock sync + ISO-8601 -> seconds-remaining.
 *
 * Two jobs, both needed only in standalone WiFi mode: give mbedTLS a real time
 * so certificate validity checks pass, and let the board turn the API's
 * absolute resets_at timestamps into live countdowns itself (over USB the PC
 * does this instead).
 */
#include <zephyr/kernel.h>
#include <zephyr/net/sntp.h>
#include <zephyr/sys/timeutil.h>
#include <zephyr/sys/printk.h>
#include <string.h>
#include <time.h>

#include "net_time.h"

static bool have_time;
/* Uptime (ms) at the moment of sync, and the Unix time then, so we can read the
 * current wall time without depending on a POSIX clock backend. */
static int64_t sync_uptime_ms;
static int64_t sync_unix_s;

int net_time_sync(int timeout_s)
{
	struct sntp_time t;
	const char *servers[] = { "pool.ntp.org", "time.google.com", "time.cloudflare.com" };

	for (int i = 0; i < 3; i++) {
		int rc = sntp_simple(servers[i], timeout_s * 1000, &t);

		if (rc == 0) {
			sync_unix_s = (int64_t)t.seconds;
			sync_uptime_ms = k_uptime_get();
			have_time = true;
			printk("[time] synced via %s (unix %lld)\n", servers[i],
			       (long long)sync_unix_s);
			return 0;
		}
		printk("[time] %s failed: %d\n", servers[i], rc);
	}
	return -ETIMEDOUT;
}

bool net_time_valid(void)
{
	return have_time;
}

static int64_t now_unix(void)
{
	return sync_unix_s + (k_uptime_get() - sync_uptime_ms) / 1000;
}

/* Parse an unsigned decimal at *p, advancing past it. -1 if no digits. */
static int parse_num(const char **p)
{
	int v = 0;
	bool any = false;

	while (**p >= '0' && **p <= '9') {
		v = v * 10 + (**p - '0');
		(*p)++;
		any = true;
	}
	return any ? v : -1;
}

/* Minimal ISO-8601 parser: "YYYY-MM-DDThh:mm:ss" with an optional fractional
 * part and an optional trailing 'Z' or +hh:mm (which we treat as UTC -- the API
 * always emits UTC). Returns Unix seconds, or -1 on malformed input.
 *
 * Hand-rolled like every other parser in this firmware: newlib's sscanf
 * faulted in scanf_ungetc the first time it ever ran on this target (CPU
 * exception, 2026-07-14), so no scanf-family calls here. */
static int64_t parse_iso(const char *s)
{
	struct tm tm = {0};
	const char *p = s;
	int y, mo, d, h, mi, sec;

	y = parse_num(&p);
	if (y < 0 || *p++ != '-') {
		return -1;
	}
	mo = parse_num(&p);
	if (mo < 0 || *p++ != '-') {
		return -1;
	}
	d = parse_num(&p);
	if (d < 0 || (*p != 'T' && *p != 't')) {
		return -1;
	}
	p++;
	h = parse_num(&p);
	if (h < 0 || *p++ != ':') {
		return -1;
	}
	mi = parse_num(&p);
	if (mi < 0 || *p++ != ':') {
		return -1;
	}
	sec = parse_num(&p);
	if (sec < 0) {
		return -1;
	}
	tm.tm_year = y - 1900;
	tm.tm_mon = mo - 1;
	tm.tm_mday = d;
	tm.tm_hour = h;
	tm.tm_min = mi;
	tm.tm_sec = sec;
	return (int64_t)timeutil_timegm(&tm);
}

int32_t net_time_secs_until(const char *iso)
{
	if (!have_time || !iso || !iso[0]) {
		return -1;
	}

	int64_t target = parse_iso(iso);

	if (target < 0) {
		return -1;
	}

	int64_t rem = target - now_unix();

	if (rem < 0) {
		rem = 0;
	}
	if (rem > INT32_MAX) {
		rem = INT32_MAX;
	}
	return (int32_t)rem;
}
