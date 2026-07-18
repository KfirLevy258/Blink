#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/display.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/reboot.h>
#include <lvgl.h>
#include <string.h>

#include "proto.h"
#include "usage_view.h"
#include "cfg_store.h"
#include "backlight.h"
#include "net_wifi.h"
#include "net_time.h"
#include "portal.h"
#include "ui_setup.h"
#include "dns_hijack.h"
#include "oauth.h"
#include "usage_client.h"
#include "ui_boot.h"
#include "ui_settings.h"
#include "ui_anim.h"
#include "tz_fetch.h"

static const struct device *const display_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));

/* Provisioning session state (one PKCE verifier per setup attempt). */
static char verifier[OAUTH_VERIFIER_LEN];
static char authorize_url[OAUTH_URL_LEN];

/*
 * The setup AP's WPA2 password: random once, persisted, carried by the QR.
 * Random rather than MAC-derived on purpose -- the MAC's vendor bytes are
 * guessable and its tail is broadcast in the SSID, so a derived password
 * would protect nothing. Factory reset clears it; the next boot rolls a
 * fresh one here.
 */
static void ap_psk_setup(void)
{
	/* No 0/O/1/l/I look-alikes: the password is only ever read by a
	 * camera, but a human may need to type it once if the QR scan is
	 * refused. */
	static const char alphabet[] = "abcdefghjkmnpqrstuvwxyz23456789";
	char psk[CFG_AP_PSK_MAX];

	if (!cfg_get_ap_psk(psk, sizeof(psk))) {
		uint8_t rnd[10];

		sys_rand_get(rnd, sizeof(rnd));
		for (int i = 0; i < 10; i++) {
			psk[i] = alphabet[rnd[i] % (sizeof(alphabet) - 1)];
		}
		psk[10] = '\0';
		cfg_set_ap_psk(psk);
	}
	net_wifi_set_ap_psk(psk);
}

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

/* The boot scan could not see the stored SSID. The join still runs -- scans
 * miss hidden SSIDs and barely-beaconing phone hotspots (an iPhone hotspot
 * with a fresh token got bounced to setup this way, 2026-07-15) -- this
 * flag only picks the honest reason to show on the setup form if that
 * join fails too. */
static bool scan_said_absent;

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

/* Same job during net_wifi's blocking waits (scan/join/DHCP/AP-up): keep
 * whatever screen is up animating. */
static void wifi_idle(void)
{
	lv_timer_handler();
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

/*
 * The one background thread. Used serially, never concurrently: the sign-in
 * exchange during provisioning, or standalone mode's network loop -- a boot
 * does one or the other. Lower priority than main (higher number): 1-2 s of
 * ECDHE math must not starve the render loop on this single-core build.
 */
#define NET_WORKER_PRIO 5

static K_THREAD_STACK_DEFINE(net_stack, 8192);	/* TLS-sized, like main's */
static struct k_thread net_thread;

/* ---- provisioning callbacks (portal owns HTTP, we own WiFi + OAuth) ---- */

/* Runs ON THE WORKER THREAD -- no LVGL calls. ui_setup_set_state() is safe
 * from here by design (volatile pending + apply on the LVGL thread), the
 * same contract the net-mgmt callbacks use. */
static int cb_sign_in(const char *code)
{
	struct oauth_tokens tok;

	ui_setup_set_state(UI_SETUP_SIGNIN, NULL);

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
	return rc;
}

/*
 * Async shell around cb_sign_in. The portal's server is single-threaded;
 * with the 15-25 s exchange run inline it went deaf mid-sign-in, /status
 * polls piled into the backlog, and the phone never learned the outcome
 * (user-reported 2026-07-16, twice). The exchange runs on the worker
 * thread instead, and the portal keeps answering polls throughout.
 */
static volatile int signin_result = 1;	/* <0 running, 0 ok, >0 fail */
static char signin_code[256];

static void signin_worker(void *a, void *b, void *c)
{
	ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
	signin_result = (cb_sign_in(signin_code) == 0) ? 0 : 1;
}

static void cb_sign_in_start(const char *code)
{
	strncpy(signin_code, code, sizeof(signin_code) - 1);
	signin_code[sizeof(signin_code) - 1] = '\0';
	signin_result = -1;
	k_thread_create(&net_thread, net_stack, K_THREAD_STACK_SIZEOF(net_stack),
			signin_worker, NULL, NULL, NULL,
			NET_WORKER_PRIO, 0, K_NO_WAIT);
}

static int cb_sign_in_poll(void)
{
	return signin_result;
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
				for (int t = 0; t < 100; t++) {	/* 1 s, screen alive */
					pump_ui();
					k_sleep(K_MSEC(10));
				}
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
				ui_boot_mark_intentional_reboot();
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
		ui_setup_set_state(UI_SETUP_WAIT, err);	/* err on-device too */
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
		/* The phone's timezone rides along with the credentials: the
		 * board's own lookup is best-effort at most (captive-ish
		 * networks eat it), and you re-provision exactly when you
		 * change timezones. Boot wires cfg -> net_time. */
		int32_t tzm;

		if (portal_last_tzmin(&tzm)) {
			cfg_set_tz(tzm);
		}
		cfg_set_wifi(ssid, psk);
		/* No "restarting" interstitial: CONNECTING goes up now, the
		 * skip splash wears the same dark background, and the resume
		 * boot re-applies CONNECTING as its very first frame -- one
		 * continuous screen across the reset (user request
		 * 2026-07-16; the old 2.5 s notice earned its keep back when
		 * the reboot was visible). */
		ui_setup_set_state(UI_SETUP_CONNECTING, ssid);
		for (int i = 0; i < 50; i++) {	/* 0.5 s, state rendered */
			pump_ui();
			k_msleep(10);
		}
		ui_boot_mark_intentional_reboot();
		sys_reboot(SYS_REBOOT_COLD);
	}
}

/* skip_join_reason: non-NULL when the caller already knows a join is doomed
 * (boot scan saw other networks but not ours) -- skip the 30 s attempt and
 * put that reason on the portal form instead. */
static void run_provisioning(const char *skip_join_reason)
{
	char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];
	bool resume = skip_join_reason == NULL &&
		      cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk));

	ui_setup_show();
	if (resume) {
		/* The first rendered frame must already be CONNECTING:
		 * letting the default scan-QR state flash for one frame
		 * betrayed the reboot (user request 2026-07-16 -- the
		 * restart should be invisible). */
		ui_setup_set_state(UI_SETUP_CONNECTING, ssid);
	}
	pump_ui();
	ui_boot_teardown();	/* boot screen freed once setup screen is live */

	oauth_gen_verifier(verifier, sizeof(verifier));
	oauth_authorize_url(verifier, authorize_url, sizeof(authorize_url));

	/* With a stored token the ack page must not promise a sign-in step --
	 * after the join, boot goes straight to the gauges. */
	char tok[CFG_TOKEN_MAX];

	portal_set_resume(cfg_get_token(tok, sizeof(tok)));

	bool joined = false;
	bool signed_in = false;
	const char *join_err = skip_join_reason;

	/* The ONLY join path: from a clean boot, before any AP mode has run
	 * this boot (a post-AP join never completes on this driver). Phase 1
	 * stores credentials and reboots into this. The CONNECTING state has
	 * been showing since the first frame above. */
	if (resume) {
		wifi_settle();
		joined = (net_wifi_connect(ssid, psk, 30) == 0);
		if (!joined) {
			join_err = net_wifi_last_error();
			/* Drop the failed pair: leaving it in NVS makes every
			 * later boot re-run this doomed join before the portal
			 * (user request 2026-07-16). The next portal round
			 * stores fresh credentials anyway. Established devices
			 * (token present) never take this branch, so an AP
			 * outage can't wipe a working setup. */
			cfg_clear_wifi();
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

	signed_in = (portal_run_signin(authorize_url, cb_sign_in_start,
				       cb_sign_in_poll, 900) == 0);
out:
	/* Reboot either way: on success come up standalone; on timeout, retry
	 * setup from a clean slate. A cold restart also reclaims all the AP/TLS
	 * memory before the standalone stack allocates it. The toast makes the
	 * restart announced instead of crash-shaped (user request 2026-07-15). */
	ui_setup_set_state(UI_SETUP_RESTART, signed_in ? "Setup complete" : NULL);
	for (int i = 0; i < 300; i++) {	/* 3 s, screen kept alive */
		pump_ui();
		k_sleep(K_MSEC(10));
	}
	ui_boot_mark_intentional_reboot();
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
			for (int t = 0; t < 100; t++) {	/* 1 s, splash alive */
				wifi_idle();
				k_sleep(K_MSEC(10));
			}
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
			ui_boot_mark_intentional_reboot();
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

/*
 * The net worker owns every blocking network call in standalone mode; the
 * main thread owns LVGL. They meet only at this queue: the worker posts UI
 * work, main applies it. NEV_WAIT strings must be static -- the pointer
 * crosses threads.
 */
struct net_evt {
	enum { NEV_STAGE, NEV_USAGE, NEV_STATUS, NEV_MODELS } kind;
	int stage;				/* NEV_STAGE */
	double s_pct, w_pct;			/* NEV_USAGE; NEV_MODELS reuses
						 * s_pct as fable's weekly */
	int32_t s_reset, w_reset;
	enum usage_status status;		/* NEV_STATUS */
};

K_MSGQ_DEFINE(net_evtq, sizeof(struct net_evt), 8, 4);

/* The boot bar's step lists -- same screen in both modes, different words
 * (user request 2026-07-16). */
static const char *const wifi_boot_steps[] = {
	"Join the WiFi",
	"Sign in to Anthropic",
	"Fetch first usage",
};
static const char *const usb_boot_steps[] = {
	"Link the PC daemon",
	"Fetch first usage",
};

static void apply_net_evt(const struct net_evt *e)
{
	switch (e->kind) {
	case NEV_STAGE:
		usage_view_boot_stage(e->stage);
		break;
	case NEV_USAGE:
		usage_view_update(e->s_pct, e->s_reset, e->w_pct, e->w_reset);
		break;
	case NEV_STATUS:
		usage_view_set_status(e->status);
		break;
	case NEV_MODELS:
		usage_view_set_models(e->s_pct);
		break;
	}
}

/* Between boot-clip frames (ui_anim_run) the mode's background duties keep
 * running through these: standalone drains the worker's queue, USB keeps
 * the serial protocol alive. */
static void standalone_anim_pump(void)
{
	struct net_evt e;

	while (k_msgq_get(&net_evtq, &e, K_NO_WAIT) == 0) {
		apply_net_evt(&e);
	}
}

static void usb_anim_pump(void)
{
	proto_service();
}

/* Lower priority than main (higher number): 1-2 s of ECDHE math must not
 * starve the render loop on this single-core build. */
static char worker_refresh[CFG_TOKEN_MAX];

static void post_stage(int stage)
{
	struct net_evt e = { .kind = NEV_STAGE, .stage = stage };

	k_msgq_put(&net_evtq, &e, K_NO_WAIT);
}

static void post_status(enum usage_status st)
{
	struct net_evt e = { .kind = NEV_STATUS, .status = st };

	k_msgq_put(&net_evtq, &e, K_NO_WAIT);
}

static void net_worker(void *a, void *b, void *c)
{
	ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);

	/* Advance the boot bar before each blocking step: visible progress on
	 * one screen instead of a title per stage (user request 2026-07-15).
	 * The clock sync rides inside the sign-in stage -- TLS needs the
	 * clock, and it is too quick to deserve a segment. */
	post_stage(1);
	net_time_sync(10);

	struct oauth_tokens tok;

	if (oauth_refresh(worker_refresh, &tok) != 0) {
		/* Refresh token rejected -- the "log in once" chain is broken.
		 * Drop it and reboot; with no token the board re-provisions,
		 * keeping the WiFi credentials. */
		cfg_clear_token();
		post_status(USAGE_STATUS_ERROR);
		k_sleep(K_SECONDS(3));
		ui_boot_mark_intentional_reboot();
		sys_reboot(SYS_REBOOT_COLD);
	}
	cfg_set_token(tok.refresh);	/* persist a rotated token before use */

	post_stage(2);

	int64_t token_deadline = k_uptime_get() + (int64_t)tok.expires_in * 1000;
	int64_t next_poll = 0;
	int64_t next_tz = 0;	/* fetch as soon as we are online */
	int32_t tz_min = 0;

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
				struct net_evt e = {
					.kind = NEV_USAGE,
					.s_pct = d.five_hour.utilization,
					.s_reset = net_time_secs_until(d.five_hour.resets_at),
					.w_pct = d.seven_day.utilization,
					.w_reset = net_time_secs_until(d.seven_day.resets_at),
				};

				k_msgq_put(&net_evtq, &e, K_NO_WAIT);

				struct net_evt m = {
					.kind = NEV_MODELS,
					.s_pct = d.seven_day_fable.present
						 ? d.seven_day_fable.utilization : -1,
				};

				k_msgq_put(&net_evtq, &m, K_NO_WAIT);
				next_poll = now + 60 * 1000;
			} else if (r == USAGE_RATE_LIMITED) {
				post_status(USAGE_STATUS_STALE);
				next_poll = now + 600 * 1000;
			} else if (r == USAGE_UNAUTHORIZED) {
				/* Token died mid-run: refresh now, retry soon. */
				oauth_refresh(tok.refresh, &tok);
				cfg_set_token(tok.refresh);
				next_poll = now + 5 * 1000;
			} else {
				post_status(USAGE_STATUS_ERROR);
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

		k_sleep(K_MSEC(250));	/* scheduling tick, not a UI pump */
	}
}

static void run_standalone(void)
{
	char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX], refresh[CFG_TOKEN_MAX];

	cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk));
	cfg_get_token(refresh, sizeof(refresh));

	usage_view_boot_begin(wifi_boot_steps, 3);
	usage_view_set_status(USAGE_STATUS_DISCONNECTED);
	lv_timer_handler();

	/* Settle even after the scan: every hotspot join that succeeded
	 * tonight ran behind this runway, and the one that skipped it
	 * dropped mid-association (2026-07-15). */
	wifi_settle();
	if (net_wifi_connect(ssid, psk, 30) != 0) {
		/* No reboot-retry dance: a failed join goes straight to the
		 * setup screen with the reason on the form (user decision
		 * 2026-07-15 -- the silent self-restarts read as crashes).
		 * The token survives, so this costs re-picking a network,
		 * never re-signing-in. */
		const char *err = scan_said_absent ? "network not found"
						   : net_wifi_last_error();

		usage_view_deinit();	/* gauges out before setup in */
		run_provisioning(err);	/* reboots when done */
	}

	/* Clock: last-known offset immediately (survives API outages), the
	 * live tz answer replaces it from the worker below. */
	int32_t tz_min = 0;

	if (cfg_get_tz(&tz_min)) {
		net_time_set_offset(tz_min);
	}

	/* Everything network-bound from here (SNTP, TLS exchanges, the 60 s
	 * poll loop) runs on the worker thread. These blocking library calls
	 * used to run right here on the UI thread: the spinner froze during
	 * every fetch and touch events overflowed their queues (user-reported
	 * 2026-07-14/15). Main now only renders. */
	strncpy(worker_refresh, refresh, sizeof(worker_refresh) - 1);
	worker_refresh[sizeof(worker_refresh) - 1] = '\0';
	k_thread_create(&net_thread, net_stack, K_THREAD_STACK_SIZEOF(net_stack),
			net_worker, NULL, NULL, NULL,
			NET_WORKER_PRIO, 0, K_NO_WAIT);

	int64_t last_tick = k_uptime_get();

	while (1) {
		struct net_evt e;

		while (k_msgq_get(&net_evtq, &e, K_NO_WAIT) == 0) {
			apply_net_evt(&e);
		}

		if (ui_anim_pending()) {
			ui_anim_run(standalone_anim_pump);
		}

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

/* ---- USB bridge mode: PC daemon pushes usage over serial ---- */

static void run_usb(void)
{
	int64_t last_tick = k_uptime_get();
	int64_t start = last_tick;
	int stage_shown = 1;

	/* With stored WiFi + token the board can serve itself if the daemon
	 * never delivers -- checked once; NVS doesn't change under us. */
	char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX], tok[CFG_TOKEN_MAX];
	bool can_fall_back = cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk)) &&
			     cfg_get_token(tok, sizeof(tok));

	while (1) {
		proto_service();

		if (ui_anim_pending()) {
			ui_anim_run(usb_anim_pump);
		}

		if (!usage_view_have_data()) {
			/* Keep the bar honest: hello was answered at boot,
			 * but proto declares the host gone after 35 s of
			 * silence -- drop back to the link step then. */
			int want = proto_host_seen() ? 1 : 0;

			if (want != stage_shown) {
				usage_view_boot_stage(want);
				stage_shown = want;
			}

			/* Waiting-for-host timeout (user request 2026-07-16):
			 * a daemon that answered hello once but has since
			 * gone silent, before ever pushing usage, is not
			 * coming back on its own. Reboot into self-service --
			 * a dead daemon won't answer the next boot's hello,
			 * so the board comes up standalone. Requires the
			 * host to be *gone*, not merely slow, so a live
			 * daemon can never reboot-loop us. */
			if (can_fall_back && !proto_host_seen() &&
			    k_uptime_get() - start > 60 * 1000) {
				printk("[usage] daemon gone before first push; standalone can serve -- rebooting\n");
				ui_boot_mark_intentional_reboot();
				sys_reboot(SYS_REBOOT_COLD);
			}
		}

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

	cfg_init();
	backlight_init();	/* drive the PWM to the persisted level */
	net_wifi_init();
	net_wifi_set_idle_hook(wifi_idle);
	ap_psk_setup();		/* before any QR or AP use */

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

	/* A mid-provisioning reboot (WiFi stored, token not yet) has exactly
	 * one destination: the setup flow. Route it straight there -- even
	 * the brief splash-colored interstitial between setup screens read
	 * as dead time on hardware (user feedback 2026-07-16). A PC daemon
	 * never loses the board to this shortcut: the daemon opening the
	 * port is a hard reset, which clears the intentional mark. */
	if (ui_boot_intentional_pending()) {
		char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX], tok[CFG_TOKEN_MAX];

		if (cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk)) &&
		    !cfg_get_token(tok, sizeof(tok))) {
			printk("[usage] mode: provisioning (resume, no splash)\n");
			run_provisioning(NULL);	/* reboots when done */
		}
	}

	ui_boot_splash();

	/* Detection, not a menu. First match wins: a talking daemon, then a
	 * reachable home network, then setup. Plugging into a PC later still
	 * works -- the daemon opening the port resets this board, so it always
	 * announces itself into a fresh splash. */
	if (proto_host_seen()) {
		printk("[usage] mode: USB bridge\n");
		usage_view_init();
		/* Same CONNECTING bar as standalone (user request
		 * 2026-07-16); the link step is already done -- the daemon
		 * spoke during the splash. */
		usage_view_boot_begin(usb_boot_steps, 2);
		usage_view_boot_stage(1);
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
		/* Take over the screen BEFORE the scan: a setup-flow reboot
		 * should land on the progress UI immediately instead of
		 * holding the splash color for the scan's 1-2 s (user
		 * feedback 2026-07-16). The scan runs under the boot bar. */
		usage_view_init();
		/* Step list before the first frame: one rendered frame of a
		 * wrong takeover flashes visibly on this panel
		 * (user-reported 2026-07-15). */
		usage_view_boot_begin(wifi_boot_steps, 3);
		lv_timer_handler();
		ui_boot_teardown();
		ui_settings_attach(lv_scr_act());

		switch (boot_ssid_scan(ssid)) {	/* may reboot (blind radio) */
		case SSID_VISIBLE:
			printk("[usage] mode: standalone WiFi\n");
			run_standalone();
			break;			/* unreachable */
		case SSID_ABSENT:
			/* Not in the scan -- but hidden SSIDs and phone
			 * hotspots rarely are. The join is the authority;
			 * the flag makes its failure decisive. */
			printk("[usage] mode: standalone WiFi (\"%s\" not in scan; probing)\n",
			       ssid);
			scan_said_absent = true;
			run_standalone();
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
