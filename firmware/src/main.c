#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include "net_wifi.h"
#include "time_sync.h"

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

int main(void)
{
	printk("[usage] firmware boot OK\n");

	if (net_wifi_connect_blocking(60) != 0) {
		LOG_ERR("WiFi bring-up failed");
		return 0;
	}
	LOG_INF("network ready");

	if (time_sync_now("pool.ntp.org", 5000) != 0) {
		LOG_ERR("time sync failed; TLS cert validation will fail");
		return 0;
	}
	LOG_INF("clock synced");

	return 0;
}
