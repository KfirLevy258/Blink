#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/display.h>
#include <zephyr/drivers/gpio.h>
#include <lvgl.h>

#include "proto.h"
#include "usage_view.h"
#include "cfg_store.h"
#include "net_wifi.h"
#include "portal.h"
#include "ui_setup.h"
#include "dns_hijack.h"

/* Called from portal_run's wait loop: keep the display alive and reflect
 * station join/leave onto the setup screen during provisioning.
 */
void portal_idle_hook(void)
{
	ui_setup_service();
	lv_timer_handler();
}

static const struct device *const display_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));
static const struct gpio_dt_spec backlight =
	GPIO_DT_SPEC_GET(DT_NODELABEL(backlight), gpios);

int main(void)
{
	printk("[usage] firmware boot OK (uart-bridge + display)\n");

	if (!device_is_ready(display_dev)) {
		printk("[usage] display not ready\n");
		return -1;
	}

	usage_view_init();
	lv_timer_handler();
	display_blanking_off(display_dev);

	/* Backlight last: the panel holds garbage until the first frame is
	 * flushed, and lighting it before then makes the boot look broken.
	 */
	if (gpio_is_ready_dt(&backlight)) {
		gpio_pin_configure_dt(&backlight, GPIO_OUTPUT_ACTIVE);
	}

	cfg_init();
	net_wifi_init();

#ifdef TEST_AP
	/* Provisioning: become an AP, serve the setup page, take what the user
	 * gives us, then join their network.
	 * TODO: the authorize URL is a placeholder until oauth.c lands.
	 */
	{
		static const char *url = "https://claude.ai/oauth/authorize?placeholder=1";
		static struct portal_result res;

		ui_setup_show();
		lv_timer_handler();

		/* Scan BEFORE becoming an AP: scanning while the SoftAP is up
		 * only sees a fraction of the nearby networks.
		 */
		static char nets[12][33];
		int nn = net_wifi_scan(nets, 12, 8);

		portal_set_networks(nets, nn > 0 ? nn : 0);

		if (net_wifi_start_ap() == 0) {
			dns_hijack_start();
		}

		if (portal_run(url, &res, 600) == 0) {
			ui_setup_status("saving...");
			lv_timer_handler();
			cfg_set_wifi(res.ssid, res.psk);
			dns_hijack_stop();
			net_wifi_stop_ap();

			ui_setup_status("connecting...");
			lv_timer_handler();
			int rc = net_wifi_connect(res.ssid, res.psk, 30);

			ui_setup_status(rc == 0 ? "connected!" : "connect FAILED");
			printk("[setup] connect rc=%d\n", rc);
			lv_timer_handler();
		}
	}
#endif

	proto_init();

	int64_t last_tick = k_uptime_get();

	while (1) {
		proto_service();

		int64_t now = k_uptime_get();

		if (now - last_tick >= 1000) {
			usage_view_tick_1s();
			last_tick = now;
		}

		lv_timer_handler();
		k_sleep(K_MSEC(10));
	}
	return 0;
}
