#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/display.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/reboot.h>
#include <lvgl.h>
#include <string.h>

#include "proto.h"
#include "usage_view.h"
#include "cfg_store.h"
#include "net_wifi.h"
#include "net_time.h"
#include "portal.h"
#include "ui_setup.h"
#include "dns_hijack.h"
#include "oauth.h"
#include "usage_client.h"

static const struct device *const display_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));
static const struct gpio_dt_spec backlight =
	GPIO_DT_SPEC_GET(DT_NODELABEL(backlight), gpios);

/* Provisioning session state (one PKCE verifier per setup attempt). */
static char verifier[OAUTH_VERIFIER_LEN];
static char authorize_url[OAUTH_URL_LEN];

static void pump_ui(void)
{
	ui_setup_service();
	lv_timer_handler();
}

/* Called from portal_run's idle wait: keep the setup screen alive. */
void portal_idle_hook(void)
{
	pump_ui();
}

/* ---- provisioning callbacks (portal owns HTTP, we own WiFi + OAuth) ---- */

static int cb_connect_wifi(const char *ssid, const char *psk)
{
	int rc = net_wifi_connect(ssid, psk, 30);

	if (rc == 0) {
		cfg_set_wifi(ssid, psk);
		/* TLS needs a real clock before the OAuth exchange. */
		net_time_sync(10);
		ui_setup_set_state(UI_SETUP_WIFI_OK, NULL);
	} else {
		ui_setup_set_state(UI_SETUP_PHONE, NULL);
	}
	pump_ui();
	return rc;
}

static int cb_sign_in(const char *code)
{
	struct oauth_tokens tok;

	ui_setup_set_state(UI_SETUP_SIGNIN, NULL);
	pump_ui();

	int rc = oauth_exchange_code(code, verifier, &tok);

	if (rc == 0) {
		cfg_set_token(tok.refresh);	/* write-before-use */
		ui_setup_set_state(UI_SETUP_DONE, NULL);
	}
	pump_ui();
	return rc;
}

static void run_provisioning(void)
{
	usage_view_deinit();		/* free the gauge screen first */
	ui_setup_show();
	pump_ui();

	oauth_gen_verifier(verifier, sizeof(verifier));
	oauth_authorize_url(verifier, authorize_url, sizeof(authorize_url));

	/* Scan before the AP comes up (scanning under SoftAP misses most APs). */
	static char nets[12][33];
	int nn = net_wifi_scan(nets, 12, 8);

	portal_set_networks(nets, nn > 0 ? nn : 0);

	if (net_wifi_start_ap() == 0) {
		dns_hijack_start();
	}

	struct portal_cb cb = {
		.authorize_url = authorize_url,
		.connect_wifi = cb_connect_wifi,
		.sign_in = cb_sign_in,
	};
	int rc = portal_run(&cb, 900);

	dns_hijack_stop();
	net_wifi_stop_ap();

	if (rc == 0) {
		cfg_set_mode(CFG_MODE_WIFI);
	}
	/* Reboot either way: on success come up standalone; on timeout, retry
	 * setup from a clean slate. A cold restart also reclaims all the AP/TLS
	 * memory before the standalone stack allocates it. */
	k_msleep(1500);
	sys_reboot(SYS_REBOOT_COLD);
}

/* ---- standalone WiFi mode: fetch usage over TLS, feed the gauges ---- */

static void run_standalone(void)
{
	char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX], refresh[CFG_TOKEN_MAX];

	cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk));
	cfg_get_token(refresh, sizeof(refresh));

	usage_view_set_status(USAGE_STATUS_DISCONNECTED);
	lv_timer_handler();

	if (net_wifi_connect(ssid, psk, 30) != 0) {
		usage_view_set_status(USAGE_STATUS_ERROR);
		k_sleep(K_SECONDS(10));
		sys_reboot(SYS_REBOOT_COLD);	/* retry from boot */
	}
	net_time_sync(10);

	struct oauth_tokens tok;

	if (oauth_refresh(refresh, &tok) != 0) {
		/* Refresh token rejected -- the "log in once" chain is broken.
		 * Drop it and reboot; with no token the board re-provisions,
		 * keeping the WiFi credentials. */
		cfg_clear_token();
		usage_view_set_status(USAGE_STATUS_ERROR);
		k_sleep(K_SECONDS(3));
		sys_reboot(SYS_REBOOT_COLD);
	}
	cfg_set_token(tok.refresh);	/* persist a rotated token before use */

	int64_t token_deadline = k_uptime_get() + (int64_t)tok.expires_in * 1000;
	int64_t next_poll = 0;
	int64_t last_tick = k_uptime_get();

	while (1) {
		int64_t now = k_uptime_get();

		/* Refresh proactively, 5 min before expiry (tokens.js rule). */
		if (now > token_deadline - 5 * 60 * 1000) {
			if (oauth_refresh(tok.refresh, &tok) == 0) {
				cfg_set_token(tok.refresh);
				token_deadline = now + (int64_t)tok.expires_in * 1000;
			}
		}

		if (now >= next_poll) {
			struct usage_data d;
			int status;
			enum usage_result r = usage_client_fetch(tok.access, &d, &status);

			if (r == USAGE_OK) {
				usage_view_update(
					d.five_hour.utilization,
					net_time_secs_until(d.five_hour.resets_at),
					d.seven_day.utilization,
					net_time_secs_until(d.seven_day.resets_at));
				next_poll = now + 300 * 1000;
			} else if (r == USAGE_RATE_LIMITED) {
				usage_view_set_status(USAGE_STATUS_STALE);
				next_poll = now + 600 * 1000;
			} else if (r == USAGE_UNAUTHORIZED) {
				/* Token died mid-run: refresh now, retry soon. */
				oauth_refresh(tok.refresh, &tok);
				cfg_set_token(tok.refresh);
				next_poll = now + 5 * 1000;
			} else {
				usage_view_set_status(USAGE_STATUS_ERROR);
				next_poll = now + 60 * 1000;
			}
		}

		if (now - last_tick >= 1000) {
			usage_view_tick_1s();
			last_tick = now;
		}
		lv_timer_handler();
		k_sleep(K_MSEC(10));
	}
}

/* ---- USB bridge mode: PC daemon pushes usage over serial ---- */

static void run_usb(void)
{
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
}

int main(void)
{
	printk("[usage] firmware boot OK\n");

	if (!device_is_ready(display_dev)) {
		printk("[usage] display not ready\n");
		return -1;
	}
	display_blanking_off(display_dev);
	if (gpio_is_ready_dt(&backlight)) {
		gpio_pin_configure_dt(&backlight, GPIO_OUTPUT_ACTIVE);
	}

	cfg_init();
	net_wifi_init();

#ifdef TEST_SCREEN
	ui_setup_show();
	for (;;) {
		const struct { enum ui_setup_state s; const char *d; } seq[] = {
			{ UI_SETUP_WAIT, NULL }, { UI_SETUP_PHONE, NULL },
			{ UI_SETUP_WIFI_OK, NULL }, { UI_SETUP_SIGNIN, NULL },
			{ UI_SETUP_DONE, NULL },
		};
		for (int i = 0; i < 5; i++) {
			ui_setup_set_state(seq[i].s, seq[i].d);
			for (int t = 0; t < 250; t++) { pump_ui(); k_sleep(K_MSEC(10)); }
		}
	}
#endif

	/* Gauge screen up front: it's what USB mode and standalone both show. */
	usage_view_init();
	lv_timer_handler();
	proto_init();

	/*
	 * Boot decision. Give a PC daemon a few seconds to speak over UART; if it
	 * does, run USB mode. Otherwise fall to WiFi: standalone if we already
	 * have credentials + a token, else first-time provisioning.
	 */
	bool usb = false;
	int64_t t0 = k_uptime_get();

	while (k_uptime_get() - t0 < 8000) {
		proto_service();
		if (proto_host_seen()) {
			usb = true;
			break;
		}
		lv_timer_handler();
		k_sleep(K_MSEC(10));
	}

	if (usb) {
		printk("[usage] mode: USB bridge\n");
		run_usb();
	}

	char tok[CFG_TOKEN_MAX], ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];
	bool have_wifi = cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk));
	bool have_tok = cfg_get_token(tok, sizeof(tok));

	if (have_wifi && have_tok) {
		printk("[usage] mode: standalone WiFi\n");
		run_standalone();
	} else {
		printk("[usage] mode: provisioning\n");
		run_provisioning();	/* reboots when done */
	}

	run_usb();	/* unreachable fallback */
	return 0;
}
