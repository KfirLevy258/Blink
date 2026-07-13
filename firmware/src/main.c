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

	/* The gauge screen and the setup screen are mutually exclusive modes and
	 * must not coexist -- with no PSRAM the LVGL heap cannot hold both, and
	 * building both at once exhausts it mid-style and faults. Create the gauge
	 * screen only on the gauge path.
	 */
	display_blanking_off(display_dev);

	/* Backlight last: the panel holds garbage until the first frame is
	 * flushed, and lighting it before then makes the boot look broken.
	 */
	if (gpio_is_ready_dt(&backlight)) {
		gpio_pin_configure_dt(&backlight, GPIO_OUTPUT_ACTIVE);
	}

	cfg_init();
	net_wifi_init();

#ifdef TEST_SCREEN
	/* Design review: cycle the setup screen through every state so the
	 * boarding-pass layout can be seen end-to-end without provisioning.
	 */
	ui_setup_show();
	for (;;) {
		const struct { enum ui_setup_state s; const char *d; } seq[] = {
			{ UI_SETUP_WAIT, NULL },
			{ UI_SETUP_PHONE, NULL },
			{ UI_SETUP_WIFI_OK, "Stone Cottage" },
			{ UI_SETUP_SIGNIN, NULL },
			{ UI_SETUP_DONE, NULL },
		};
		for (int i = 0; i < 5; i++) {
			ui_setup_set_state(seq[i].s, seq[i].d);
			for (int t = 0; t < 250; t++) {
				ui_setup_service();
				lv_timer_handler();
				k_sleep(K_MSEC(10));
			}
		}
	}
#endif

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
			cfg_set_wifi(res.ssid, res.psk);
			dns_hijack_stop();
			net_wifi_stop_ap();

			ui_setup_set_state(UI_SETUP_WIFI_OK, res.ssid);
			lv_timer_handler();
			int rc = net_wifi_connect(res.ssid, res.psk, 30);

			ui_setup_set_state(rc == 0 ? UI_SETUP_DONE : UI_SETUP_ERROR,
					   rc == 0 ? NULL : "WiFi failed");
			printk("[setup] connect rc=%d\n", rc);
			lv_timer_handler();
		}
	}
#endif

	/* Gauge/USB path: build the gauge screen now (the setup screen, if any, is
	 * gone by here) and start the UART bridge.
	 */
	usage_view_init();
	lv_timer_handler();

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
