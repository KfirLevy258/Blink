/*
 * Wall-clock time on the board. The daemon's `time` message carries a Unix
 * epoch, which is pinned to k_uptime here; the local UTC offset rides along
 * with it, and ISO-8601 reset stamps are turned into seconds left.
 *
 * The "net" in the name dates from the standalone WiFi mode, which set this
 * clock over SNTP so mbedTLS could check certificate dates and the board
 * could count down to the API's resets_at on its own. The mode was deleted
 * on 2026-10-27; the name stayed, since a rename would only churn includes.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/timeutil.h>
#include <zephyr/sys/printk.h>
#include <string.h>
#include <time.h>

#include "net_time.h"
#include "version.h"

/* Sanity window for any accepted time source. The floor blocks the
 * clock-rollback certificate attack (see BLINK_TIME_FLOOR); the ceiling
 * matches the protocol's "before 2100" bound and catches garbage. */
#define TIME_CEILING 4102444800LL	/* 2100-01-01T00:00:00Z */

static bool time_sane(int64_t unix_s)
{
	return unix_s >= BLINK_TIME_FLOOR && unix_s < TIME_CEILING;
}

static bool have_time;
/* Uptime (ms) at the moment of sync, and the Unix time then, so we can read the
 * current wall time without depending on a POSIX clock backend. */
static int64_t sync_uptime_ms;
static int64_t sync_unix_s;

static bool have_offset;
static int32_t offset_min;

/* sync_uptime_ms and sync_unix_s are 64-bit each, and the Xtensa LX6 stores
 * 32 bits at a time, so a reader on another thread could see half of an
 * update. That other thread was the WiFi mode's SNTP worker, removed
 * 2026-10-27. Today the daemon's `time` message is parsed in proto_service()
 * and the clock read by the 1 s tick, both on the main loop, so the lock is
 * not contended; it stays because it costs nanoseconds and keeps a future
 * second caller safe without anyone having to remember this. */
static struct k_spinlock time_lock;

bool net_time_valid(void)
{
	return have_time;
}

static int64_t now_unix(void)
{
	k_spinlock_key_t key = k_spin_lock(&time_lock);
	int64_t v = sync_unix_s + (k_uptime_get() - sync_uptime_ms) / 1000;

	k_spin_unlock(&time_lock, key);
	return v;
}

void net_time_set_manual(int64_t unix_s)
{
	if (!time_sane(unix_s)) {
		/* The USB daemon's time push gets the same scrutiny as SNTP:
		 * the serial line is exactly as unauthenticated. */
		printk("[time] manual time %lld outside sane window; ignored\n",
		       (long long)unix_s);
		return;
	}

	k_spinlock_key_t key = k_spin_lock(&time_lock);

	sync_unix_s = unix_s;
	sync_uptime_ms = k_uptime_get();
	have_time = true;
	k_spin_unlock(&time_lock, key);
}

void net_time_set_offset(int32_t om)
{
	offset_min = om;
	have_offset = true;
}

bool net_time_local(int *hh, int *mm)
{
	if (!have_time || !have_offset) {
		return false;
	}

	int64_t sec_of_day = (now_unix() + (int64_t)offset_min * 60) % 86400;

	if (sec_of_day < 0) {
		sec_of_day += 86400;	/* western offsets near midnight */
	}
	*hh = (int)(sec_of_day / 3600);
	*mm = (int)((sec_of_day % 3600) / 60);
	return true;
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
