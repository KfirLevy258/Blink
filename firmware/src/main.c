#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include "net_wifi.h"
#include "time_sync.h"
#include "usage_client.h"

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

#define POLL_SECONDS    300
#define BACKOFF_SECONDS 600   /* on HTTP 429 */

static void print_usage(const struct usage_data *d, int status)
{
	printk("[usage] HTTP %d\n", status);
	printk("[usage] Session (5h): %5.1f%%   resets %s\n",
	       d->five_hour.utilization, d->five_hour.resets_at);
	printk("[usage] Weekly  (7d): %5.1f%%   resets %s\n",
	       d->seven_day.utilization, d->seven_day.resets_at);
	if (d->seven_day_sonnet.present) {
		printk("[usage] Weekly Sonnet: %4.1f%%\n",
		       d->seven_day_sonnet.utilization);
	}
}

int main(void)
{
	printk("[usage] firmware boot OK\n");

	if (net_wifi_connect_blocking(60) != 0) {
		LOG_ERR("WiFi bring-up failed");
		return 0;
	}
	LOG_INF("network ready");

	if (time_sync_now("pool.ntp.org", 5000) != 0) {
		LOG_ERR("time sync failed; aborting (TLS needs a real clock)");
		return 0;
	}
	LOG_INF("clock synced");

	usage_client_init_ca();

	while (1) {
		if (!net_wifi_has_ip()) {
			LOG_WRN("WiFi link down; reconnecting");
			if (net_wifi_connect_blocking(60) != 0) {
				k_sleep(K_SECONDS(30));
				continue;
			}
		}

		struct usage_data d;
		int status = 0;
		enum usage_result r = usage_client_fetch(&d, &status);
		int wait = POLL_SECONDS;

		switch (r) {
		case USAGE_OK:
			print_usage(&d, status);
			break;
		case USAGE_RATE_LIMITED:
			printk("[usage] HTTP 429 rate-limited; backing off\n");
			wait = BACKOFF_SECONDS;
			break;
		case USAGE_UNAUTHORIZED:
			printk("[usage] HTTP 401 — token expired; paste a fresh CLAUDE_TOKEN\n");
			break;
		default:
			printk("[usage] fetch error (r=%d http=%d); will retry\n", r, status);
			break;
		}

		printk("[usage] next poll in %ds\n", wait);
		k_sleep(K_SECONDS(wait));
	}
	return 0;
}
