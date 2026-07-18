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
#include "version.h"

/* Sanity window for any accepted time source. The floor blocks the
 * clock-rollback certificate attack (see CLAUGE_TIME_FLOOR); the ceiling
 * matches the protocol's "before 2100" bound and catches garbage. */
#define TIME_CEILING 4102444800LL	/* 2100-01-01T00:00:00Z */

static bool time_sane(int64_t unix_s)
{
	return unix_s >= CLAUGE_TIME_FLOOR && unix_s < TIME_CEILING;
}

static bool have_time;
/* Uptime (ms) at the moment of sync, and the Unix time then, so we can read the
 * current wall time without depending on a POSIX clock backend. */
static int64_t sync_uptime_ms;
static int64_t sync_unix_s;

static bool have_offset;
static int32_t offset_min;

/* The standalone net worker syncs/adjusts time while the UI thread reads the
 * clock every second. The base is a 64-bit pair -- not atomic on this core --
 * so reads and writes go through a spinlock (held for nanoseconds). */
static struct k_spinlock time_lock;

int net_time_sync(int timeout_s)
{
	struct sntp_time t;
	const char *servers[] = { "pool.ntp.org", "time.google.com", "time.cloudflare.com" };

	for (int i = 0; i < 3; i++) {
		int rc = sntp_simple(servers[i], timeout_s * 1000, &t);

		if (rc == 0 && !time_sane((int64_t)t.seconds)) {
			/* A "successful" sync outside the sane window is
			 * worse than none: TLS would then trust certificates
			 * against a lie. Try the next server. */
			printk("[time] %s answered insane time %lld; rejected\n",
			       servers[i], (long long)t.seconds);
			rc = -EINVAL;
		}
		if (rc == 0) {
			k_spinlock_key_t key = k_spin_lock(&time_lock);

			sync_unix_s = (int64_t)t.seconds;
			sync_uptime_ms = k_uptime_get();
			have_time = true;
			k_spin_unlock(&time_lock, key);
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
