#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/display.h>
#include <zephyr/drivers/gpio.h>
#include <lvgl.h>

#include "proto.h"
#include "usage_view.h"
#include "cfg_store.h"
#include "net_wifi.h"

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
	/* TEMPORARY: prove SoftAP works on real hardware before building the
	 * provisioning portal on top of it.
	 */
	net_wifi_start_ap();
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
