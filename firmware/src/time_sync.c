#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/sntp.h>
#include <time.h>
#include <sys/time.h>

#include "time_sync.h"

LOG_MODULE_REGISTER(time_sync, LOG_LEVEL_INF);

int time_sync_now(const char *server, int timeout_ms)
{
	struct sntp_time ts;
	int ret = sntp_simple(server, timeout_ms, &ts);

	if (ret < 0) {
		LOG_ERR("SNTP query failed: %d", ret);
		return ret;
	}

	struct timespec tspec = {
		.tv_sec = (time_t)ts.seconds,
		.tv_nsec = 0,
	};
	if (clock_settime(CLOCK_REALTIME, &tspec) != 0) {
		LOG_ERR("clock_settime failed");
		return -EIO;
	}

	time_t now = (time_t)ts.seconds;
	LOG_INF("time set (epoch %lld): %s", (long long)now, ctime(&now));
	return 0;
}
