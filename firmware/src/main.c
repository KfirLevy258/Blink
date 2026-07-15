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
#include "ui_boot.h"
#include "ui_settings.h"
#include "tz_fetch.h"

static const struct device *const display_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));
static const struct gpio_dt_spec backlight =
	GPIO_DT_SPEC_GET(DT_NODELABEL(backlight), gpios);

/* Provisioning session state (one PKCE verifier per setup attempt). */
static char verifier[OAUTH_VERIFIER_LEN];
static char authorize_url[OAUTH_URL_LEN];

/*
 * The radio sometimes comes out of a reset blind: every scan returns nothing
 * and joins time out, for that entire boot; another reboot re-rolls it
 * (observed on this hardware 2026-07-13, roughly every other soft reset).
 * These counters live in .noinit RAM so they survive the reboot the retry
 * depends on; the magic guards against power-on garbage.
 */
static __noinit uint32_t blind_boots;
static __noinit uint32_t blind_magic;
#define BLIND_MAGIC 0xb11dbea7u
#define BLIND_MAX 4

/*
 * Standalone join failures, counted across the reboot-retry the driver
 * demands (joins only complete from a clean boot). One failure is usually
 * the router rebooting or a DHCP hiccup; a STREAK means we moved or the
 * password changed, and only then is the setup portal the right answer.
 * A scan cannot make this call: this unit's scans provably miss real,
 * joinable networks (Stone Cottage, 2026-07-14), as hidden SSIDs always
 * would. The join itself is the only authoritative probe.
 */
static __noinit uint32_t join_fails;
static __noinit uint32_t join_magic;
#define JOIN_MAGIC 0x104e4a01u
#define JOIN_MAX 2

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

/*
 * Radio settle before the first join of a boot. The pre-redesign flow never
 * joined before ~12 s of uptime (8 s sniff + selection screen) and connected
 * every time; the detection flow's ~3 s join failed with a mid-handshake
 * disconnect on every boot tried (2026-07-14). The driver evidently needs
 * runway after init. Keeps the UI pumping so the spinner stays alive.
 */
static void wifi_settle(void)
{
	for (int i = 0; i < 500; i++) {	/* 5 s */
		lv_timer_handler();
		k_sleep(K_MSEC(10));
	}
}

/* ---- provisioning callbacks (portal owns HTTP, we own WiFi + OAuth) ---- */

static int cb_sign_in(const char *code)
{
	struct oauth_tokens tok;

	ui_setup_set_state(UI_SETUP_SIGNIN, NULL);
	pump_ui();

	/* TLS certificate checks need a real wall clock; sync it now, the first
	 * time we actually talk to Anthropic. */
	if (!net_time_valid()) {
		net_time_sync(10);
	}

	int rc = oauth_exchange_code(code, verifier, &tok);

	if (rc == 0) {
		cfg_set_token(tok.refresh);	/* write-before-use */
		ui_setup_set_state(UI_SETUP_DONE, NULL);
	} else {
		ui_setup_set_state(UI_SETUP_ERROR, NULL);
	}
	pump_ui();
	return rc;
}

/*
 * Phase 1: run the AP until credentials arrive, then tear it down and try to
 * join. On failure, restart the AP with the reason on the form and let the
 * user rejoin the setup network. Returns 0 with creds stored, or negative on
 * portal timeout / AP failure (caller reboots either way).
 */
static int phase1_get_wifi(char *ssid, size_t slen, char *psk, size_t plen,
			   const char *err0)
{
	const char *err = err0;
	bool first_round = true;

	for (;;) {
		/* Scan before the AP comes up (scanning under SoftAP misses
		 * most APs). Re-scan each round: the failure may have been
		 * "network not found". */
		static char nets[12][33];
		int nn = 0;

		for (int attempt = 0; attempt < 3 && nn <= 0; attempt++) {
			if (attempt > 0) {
				k_msleep(1000);
			}
			nn = net_wifi_scan(nets, 12, 8);
		}

		if (blind_magic != BLIND_MAGIC) {
			blind_magic = BLIND_MAGIC;
			blind_boots = 0;
		}
		if (nn > 0) {
			blind_boots = 0;
		} else if (first_round) {
			/* Blind boot. Reboot to re-roll the radio rather than
			 * offer an empty network list. Bounded: after BLIND_MAX
			 * consecutive blind boots, give up and serve the portal
			 * anyway (its own 15-min timeout starts a new streak),
			 * so a location with no networks can't reboot-loop. */
			if (blind_boots < BLIND_MAX) {
				blind_boots++;
				printk("[wifi] radio blind (0 scans); reboot %u/%u to re-roll\n",
				       blind_boots, BLIND_MAX);
				k_msleep(300);
				sys_reboot(SYS_REBOOT_COLD);
			}
			blind_boots = 0;
		}
		first_round = false;

		portal_set_networks(nets, nn > 0 ? nn : 0);

		int rc = net_wifi_start_ap();

		if (rc != 0) {
			return rc;
		}
		dns_hijack_start();
		ui_setup_set_state(UI_SETUP_WAIT, NULL);
		pump_ui();

		rc = portal_run_wifi(ssid, slen, psk, plen, err, 900);

		dns_hijack_stop();
		net_wifi_stop_ap();
		if (rc != 0) {
			return rc;	/* nobody set us up in time */
		}

		/*
		 * Do NOT join here. On this driver a station brought up right
		 * after AP_DISABLE accepts the connect request but never
		 * completes it -- every post-AP join timed out on hardware
		 * (3/3), while the same credentials joined instantly from a
		 * fresh boot (resume path). So persist and reboot; the boot
		 * path does the join. A wrong password self-corrects: the
		 * resume join fails and lands back here with the reason on
		 * the form.
		 */
		cfg_set_wifi(ssid, psk);
		ui_setup_set_state(UI_SETUP_CONNECTING, ssid);
		pump_ui();
		k_msleep(500);
		sys_reboot(SYS_REBOOT_COLD);
	}
}

/* skip_join_reason: non-NULL when the caller already knows a join is doomed
 * (boot scan saw other networks but not ours) -- skip the 30 s attempt and
 * put that reason on the portal form instead. */
static void run_provisioning(const char *skip_join_reason)
{
	ui_setup_show();
	pump_ui();
	ui_boot_teardown();	/* boot screen freed once setup screen is live */

	oauth_gen_verifier(verifier, sizeof(verifier));
	oauth_authorize_url(verifier, authorize_url, sizeof(authorize_url));

	char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];
	bool joined = false;
	const char *join_err = skip_join_reason;

	/* The ONLY join path: from a clean boot, before any AP mode has run
	 * this boot (a post-AP join never completes on this driver). Phase 1
	 * stores credentials and reboots into this. */
	if (skip_join_reason == NULL &&
	    cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk))) {
		ui_setup_set_state(UI_SETUP_CONNECTING, ssid);
		pump_ui();
		wifi_settle();
		joined = (net_wifi_connect(ssid, psk, 30) == 0);
		if (!joined) {
			join_err = net_wifi_last_error();
		}
	}

	if (!joined) {
		/* Collects credentials over the AP, then reboots (or returns
		 * on timeout/AP failure -- reboot then too, via out). */
		phase1_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk), join_err);
		goto out;
	}

	/* Phase 2: sign-in over the home LAN. */
	char ip[16], url[32];

	if (!net_wifi_sta_ip(ip, sizeof(ip))) {
		goto out;
	}
	snprintf(url, sizeof(url), "http://%s/", ip);
	printk("[usage] sign-in page at %s\n", url);
	ui_setup_set_state(UI_SETUP_WIFI_OK, url);
	pump_ui();

	portal_run_signin(authorize_url, cb_sign_in, 900);
out:
	/* Reboot either way: on success come up standalone; on timeout, retry
	 * setup from a clean slate. A cold restart also reclaims all the AP/TLS
	 * memory before the standalone stack allocates it. */
	k_msleep(1500);
	sys_reboot(SYS_REBOOT_COLD);
}

/*
 * Is the stored network on the air? First live run of this gate was right
 * and got misread: it reported the SSID absent minutes after the device
 * moved homes (2026-07-14), and the "scan must be broken" fix that replaced
 * it made a move cost three failed-join boot cycles before the portal.
 * Scan first: an answer in ~8 s. Same blind-radio discipline as the
 * provisioning scan -- an empty result is bimodal radio luck, not evidence
 * of absence, so re-roll by rebooting (bounded); only a scan that SEES
 * networks, just not ours, gets to say "absent". Known limit: a hidden SSID
 * would always read absent and live at the portal.
 */
enum ssid_scan { SSID_VISIBLE, SSID_ABSENT, SSID_RADIO_BLIND };

static enum ssid_scan boot_ssid_scan(const char *ssid)
{
	static char nets[12][33];
	int nn = 0;

	for (int attempt = 0; attempt < 3 && nn <= 0; attempt++) {
		if (attempt > 0) {
			k_msleep(1000);
		}
		nn = net_wifi_scan(nets, 12, 8);
	}

	if (blind_magic != BLIND_MAGIC) {
		blind_magic = BLIND_MAGIC;
		blind_boots = 0;
	}
	if (nn <= 0) {
		if (blind_boots < BLIND_MAX) {
			blind_boots++;
			printk("[wifi] radio blind (0 scans); reboot %u/%u to re-roll\n",
			       blind_boots, BLIND_MAX);
			k_msleep(300);
			sys_reboot(SYS_REBOOT_COLD);
		}
		/* Persistently blind: stop guessing. The portal (typed-SSID
		 * field) is the honest next step, and its timeout-reboot
		 * starts a fresh streak. */
		blind_boots = 0;
		return SSID_RADIO_BLIND;
	}
	blind_boots = 0;

	for (int i = 0; i < nn; i++) {
		if (strcmp(nets[i], ssid) == 0) {
			return SSID_VISIBLE;
		}
	}
	return SSID_ABSENT;
}

/* ---- standalone WiFi mode: fetch usage over TLS, feed the gauges ---- */

static void run_standalone(void)
{
	char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX], refresh[CFG_TOKEN_MAX];

	cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk));
	cfg_get_token(refresh, sizeof(refresh));

	/* The takeover's default text asks for a PC daemon -- wrong story in
	 * this mode (seen on hardware 2026-07-14). Say what we actually do. */
	usage_view_set_waiting("CONNECTING", "joining WiFi, first fetch on its way");
	usage_view_set_status(USAGE_STATUS_DISCONNECTED);
	lv_timer_handler();

	/* No settle here: the boot scan (~8 s) that gated this path already
	 * gave the radio its runway. */
	if (net_wifi_connect(ssid, psk, 30) != 0) {
		if (join_magic != JOIN_MAGIC) {
			join_magic = JOIN_MAGIC;
			join_fails = 0;
		}
		if (++join_fails <= JOIN_MAX) {
			/* Probably transient; the retry must come from a
			 * clean boot on this driver. */
			usage_view_set_status(USAGE_STATUS_ERROR);
			k_sleep(K_SECONDS(10));
			sys_reboot(SYS_REBOOT_COLD);
		}
		/* A streak: moved, network gone, or password changed. Offer
		 * setup with the reason on the form instead of rebooting
		 * forever. */
		join_fails = 0;

		const char *err = net_wifi_last_error();

		usage_view_deinit();	/* 16K pool: gauges out before setup in */
		run_provisioning(err);	/* reboots when done */
	}
	if (join_magic == JOIN_MAGIC) {
		join_fails = 0;		/* success ends any streak */
	}
	net_time_sync(10);

	/* Clock: last-known offset immediately (survives API outages), the
	 * live answer replaces it below. */
	int32_t tz_min = 0;

	if (cfg_get_tz(&tz_min)) {
		net_time_set_offset(tz_min);
	}

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
	int64_t next_tz = 0;	/* fetch as soon as we are online */
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
				next_poll = now + 60 * 1000;
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

		if (now >= next_tz) {
			int32_t om;

			if (tz_fetch_offset(&om) == 0) {
				net_time_set_offset(om);
				if (!cfg_get_tz(&tz_min) || tz_min != om) {
					cfg_set_tz(om);	/* new last-known */
				}
				next_tz = now + 86400LL * 1000;	/* daily: tracks DST */
			} else {
				next_tz = now + 3600LL * 1000;	/* retry hourly */
			}
		}

		if (now - last_tick >= 1000) {
			usage_view_tick_1s();
			last_tick = now;

			int hh = -1, mm = 0;

			net_time_local(&hh, &mm);
			usage_view_set_clock(hh, mm);
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

			int hh = -1, mm = 0;

			net_time_local(&hh, &mm);
			usage_view_set_clock(hh, mm);
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

	/* proto first: its hello goes out now, so a daemon's reply is already
	 * in flight during the splash and the selection can short-circuit. */
	proto_init();

	ui_boot_splash();

	/* Detection, not a menu. First match wins: a talking daemon, then a
	 * reachable home network, then setup. Plugging into a PC later still
	 * works -- the daemon opening the port resets this board, so it always
	 * announces itself into a fresh splash. */
	if (proto_host_seen()) {
		printk("[usage] mode: USB bridge\n");
		usage_view_init();
		lv_timer_handler();
		ui_boot_teardown();	/* only after the new screen is loaded */
		ui_settings_attach(lv_scr_act());
		usage_view_set_status(USAGE_STATUS_DISCONNECTED);
		proto_resync();		/* daemon re-pushes time+usage right away */
		run_usb();
	}

	char tok[CFG_TOKEN_MAX], ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];
	bool have_wifi = cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk));
	bool have_tok = cfg_get_token(tok, sizeof(tok));

	if (have_wifi && have_tok) {
		switch (boot_ssid_scan(ssid)) {	/* may reboot (blind radio) */
		case SSID_VISIBLE:
			/* Join-failure strikes inside run_standalone remain
			 * the backstop for a scan that said yes wrongly. */
			printk("[usage] mode: standalone WiFi\n");
			usage_view_init();
			lv_timer_handler();
			ui_boot_teardown();
			ui_settings_attach(lv_scr_act());
			run_standalone();
			break;			/* unreachable */
		case SSID_ABSENT:
			printk("[usage] mode: provisioning (\"%s\" not visible)\n",
			       ssid);
			run_provisioning("network not found");
			break;			/* unreachable */
		case SSID_RADIO_BLIND:
			printk("[usage] mode: provisioning (radio blind)\n");
			run_provisioning("no networks found; check antenna or power");
			break;			/* unreachable */
		}
	}

	printk("[usage] mode: provisioning\n");
	run_provisioning(NULL);	/* reboots when done */
	return 0;
}
