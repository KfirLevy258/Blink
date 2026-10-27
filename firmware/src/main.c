#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/display.h>
#include <zephyr/drivers/mipi_dbi.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/reboot.h>
#include <lvgl.h>
#include <mbedtls/platform.h>
#include <string.h>
#include <errno.h>

#include "proto.h"
#include "usage_view.h"
#include "cfg_store.h"
#include "ui_theme.h"
#include "backlight.h"
#include "net_time.h"
#include "ui_boot.h"
#include "fatal.h"
#include "ui_sleep.h"
#include "sleep_gate.h"
#include "usage_freshness.h"
#include "ui_settings.h"
#include "ui_anim.h"
#include "ota.h"
#include "upd_tap.h"
#include "whatsnew.h"
#include "ui_touchfx.h"
#include "version.h"
#include "mem_stats.h"

static const struct device *const display_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));

/*
 * MADCTL correction for the production batch.
 *
 * These panels differ from the pilot units in two independent ways, and both
 * land in MADCTL (36h), so one write fixes both. Pilot boards must build with
 * CONFIG_BLINK_PANEL_PILOT, which compiles this away entirely.
 *
 * The pilot units and the production panels are driven with byte-identical
 * registers, yet production renders mirrored -- so the difference is in the
 * module, not in anything we send. The usual cure for that, the SS bit in
 * DISCTRL (B6h), was tried on hardware 2026-08-21: the built devicetree
 * carried it (disctrl = [0a a2 27 04]) and the image did not budge, which
 * rules the B6h route out on this controller. MADCTL is honoured, so the
 * compensation lives there instead.
 *
 * It cannot be a devicetree `rotation`. The ili9xxx driver's CMD_SET_1 table
 * reaches only MV (90 deg) and MV|MX|MY (270 deg) in landscape, and those two
 * differ by a pure 180 deg turn -- neither un-mirrors. What this panel needs is
 * MV|MY, which no rotation value can produce. So we re-issue MADCTL ourselves
 * once, straight onto the DBI bus, after the driver has finished its init.
 *
 * Second, the colour order. The driver hardcodes the BGR bit for every
 * orientation, but these panels are wired RGB, so red and blue arrived
 * swapped. Measured 2026-08-21 with a six-bar test pattern of known values:
 * the RED bar read blue and the BLUE bar read red, while GREEN stayed green
 * and white/black were exact -- a clean channel exchange, with no inversion
 * or gamma error. Clearing bit 3 restores it. (Green surviving a red/blue
 * swap untouched is why eyeballing a green UI element misdiagnosed this.)
 *
 * Safe to overwrite: the driver writes MADCTL only from set_orientation(),
 * which runs during init and is never called again (nothing in the LVGL glue
 * touches orientation), so this value is the one that sticks. Partial updates
 * stay correct because MY flips the row-address mapping wholesale -- LVGL's
 * window coordinates and its pixels mirror together, and the panel's own
 * reversed wiring cancels them back out.
 */
#ifdef CONFIG_BLINK_PANEL_PILOT

/* Pilot panels need none of it: the stock rotation=90 MADCTL (BGR|MV) that the
 * driver writes is already correct for them, mirror and colour order alike. */
static void panel_fix_madctl(void)
{
}

#else

#define PANEL_MADCTL_MV   BIT(5)
#define PANEL_MADCTL_MY   BIT(7)

static const struct device *const panel_dbi =
	DEVICE_DT_GET(DT_PARENT(DT_NODELABEL(ili9341)));
static const struct mipi_dbi_config panel_dbi_cfg =
	MIPI_DBI_CONFIG_DT(DT_NODELABEL(ili9341),
			   SPI_OP_MODE_MASTER | SPI_WORD_SET(8), 0);

static void panel_fix_madctl(void)
{
	uint8_t madctl = PANEL_MADCTL_MV | PANEL_MADCTL_MY;	/* BGR bit clear = RGB */
	int r;

	if (!device_is_ready(panel_dbi)) {
		printk("[usage] panel: DBI bus not ready, MADCTL uncorrected\n");
		return;
	}

	r = mipi_dbi_command_write(panel_dbi, &panel_dbi_cfg, 0x36, &madctl, 1);
	if (r < 0) {
		printk("[usage] panel: MADCTL write failed (%d)\n", r);
	} else {
		printk("[usage] panel: MADCTL=0x%02x\n", madctl);
	}
}

#endif /* CONFIG_BLINK_PANEL_PILOT */

/* ---- OTA boot-side: test-boot self-confirm, else MCUboot reverts ---- */

static bool ota_test_boot;
static bool ota_health;		/* set once usage has arrived over USB */
static int64_t ota_confirm_deadline;
static const struct device *wdt;
static int wdt_chan = -1;

static void ota_boot_begin(void)
{
	if (boot_is_img_confirmed()) {
		return;
	}
	ota_test_boot = true;
	ota_confirm_deadline = k_uptime_get() + 90 * 1000;
	printk("[ota] test boot of %s -- must confirm within 90 s\n",
	       BLINK_FW_VERSION);

	/* Hardware watchdog for hard hangs: a wedged main loop stops feeding,
	 * the chip resets, and MCUboot reverts the unconfirmed image. */
	wdt = DEVICE_DT_GET_OR_NULL(DT_ALIAS(watchdog0));
	if (wdt && device_is_ready(wdt)) {
		struct wdt_timeout_cfg cfg = {
			.window.max = 30000,
			.flags = WDT_FLAG_RESET_SOC,
		};

		wdt_chan = wdt_install_timeout(wdt, &cfg);
		if (wdt_chan >= 0) {
			wdt_setup(wdt, 0);
		}
	}
}

static void ota_boot_pump(void)
{
	if (!ota_test_boot) {
		return;
	}
	if (wdt_chan >= 0) {
		wdt_feed(wdt, wdt_chan);	/* alive != healthy; deadline judges health */
	}
	if (ota_health) {
		boot_write_img_confirmed();
		ota_test_boot = false;
		if (wdt_chan >= 0) {
			wdt_disable(wdt);
			wdt_chan = -1;
		}
		printk("[ota] image confirmed\n");
		return;
	}
	if (k_uptime_get() > ota_confirm_deadline) {
		printk("[ota] not healthy within 90 s -- rebooting to revert\n");
		ui_boot_mark_intentional_reboot();
		sys_reboot(SYS_REBOOT_COLD);
	}
}

/*
 * "Do not close this board's eyes right now", in one place.
 *
 * There were two hand-written copies of this condition and they had already
 * drifted: the doze in run_usb() carried ota_test_boot, and
 * reading_moved_again() -- whose own comment calls it the mirror of that
 * doze -- did not. The drift is unreachable today, because ota_test_boot is
 * raised once in ota_boot_begin() before run_usb() exists and from then
 * on only ever falls. But it is exactly the term whose absence cost a board
 * on the bench (2026-09-03): dozing blocks the loop that feeds the 30 s
 * watchdog an unconfirmed image arms, the SoC resets, MCUboot re-runs the
 * same unconfirmed image, and it dozes again at 60 s -- forever. A condition
 * that has to be right in two files to keep a board out of a reset loop
 * should exist once.
 *
 * "An update is in flight" honestly covers "the new image has not proved
 * itself yet", which is why the test boot rides this term instead of
 * becoming a fourth condition at each call site; staying awake is also what
 * lets the deliberate revert at the 90 s deadline happen, logged, rather
 * than as a bare watchdog reset nobody can read afterwards.
 *
 * Read fresh on every call, never cached: an update that starts while the
 * board is already dozing has to put the progress screen back up.
 */
static bool ota_blocks_sleep(void)
{
	struct ota_ui snap;

	ota_ui_get(&snap);
	return snap.st == OTA_UI_DOWNLOADING || snap.st == OTA_UI_REBOOTING ||
	       ota_test_boot;
}

/* Did the previous boot's install land? Runs once the gauge screen exists so
 * the notice popup has somewhere to live. */
static void ota_report_outcome(void)
{
	char trail[CFG_OTA_VER_MAX];
	char from[CFG_OTA_VER_MAX], to[CFG_OTA_VER_MAX];
	char summary[WHATSNEW_SUMMARY_MAX];

	if (cfg_get_ota_state(trail, sizeof(trail)) != 1) {
		return;
	}
	cfg_set_ota_state(0, "");
	/* Through whatsnew_split, always: the breadcrumb may be a packed
	 * pair, and comparing "1.2.5>1.3.2" against BLINK_FW_VERSION would
	 * announce a successful update as a failed one. */
	whatsnew_split(trail, from, sizeof(from), to, sizeof(to));
	/*
	 * The board's own verdict on the update, in the console log.
	 *
	 * It had none. The daemon logs that it flashed an image and the panel
	 * puts up a popup that is gone as soon as anyone taps OK, so the one
	 * question support actually asks after a failed update -- did the
	 * board come up on the version it was aiming at -- had no answer
	 * anywhere afterwards.
	 *
	 * It also makes this decision observable on a bench. That matters
	 * more than it looks: reading the breadcrumb wrongly reports a
	 * SUCCESSFUL update as a failure, silently, and the popup it puts up
	 * is the only other evidence.
	 */
	printk("[ota] update %s: wanted %s, running %s (from %s)\n",
	       strcmp(to, BLINK_FW_VERSION) == 0 ? "landed" : "REVERTED",
	       to, BLINK_FW_VERSION, from[0] ? from : "?");
	if (strcmp(to, BLINK_FW_VERSION) == 0) {
		/*
		 * The notice says how much changed; the screen behind it says
		 * what. An empty summary means this release has no entry in
		 * the table, and then the notice shows the tick and the
		 * version alone rather than offering a screen with nothing
		 * on it.
		 */
		whatsnew_summary(from, BLINK_FW_VERSION, summary,
				 sizeof(summary));
		ui_settings_notice_update(BLINK_FW_VERSION, summary, from,
					  BLINK_FW_VERSION);
	} else {
		ui_settings_notice("Update failed, previous version restored.");
	}
}

/* Step names for the CONNECTING bar, in order. */
static const char *const usb_boot_steps[] = {
	"Link the PC daemon",
	"Fetch first usage",
};

static void usb_anim_pump(void)
{
	proto_service();
	if (usage_view_have_data()) {
		ota_health = true;	/* daemon delivered usage */
	}
	ota_boot_pump();
}

/* ---- The one mode: the PC daemon pushes usage over serial ---- */

/* The original reason to wake: the app said something. proto.c clears
 * host_seen on the timeout that armed the sleep in the first place, so this
 * is false throughout the doze and true on the first line back. */
static bool host_is_back(void)
{
	return proto_host_seen();
}

/*
 * The other reason to wake: something new to show.
 *
 * The mirror of sleep_stale_should_start's non-had_usage terms, and it has
 * to be exactly that -- tests/sleep_gate pins the two as complements over a
 * grid, because a wake condition a second away from the sleep condition
 * would have this board closing and opening its eyes forever on a real desk.
 *
 * It says mirror and now it is one: the OTA term comes from
 * ota_blocks_sleep(), the same call the doze arms on. It used to be spelled
 * out here a second time, and the second spelling was missing ota_test_boot.
 */
static bool reading_moved_again(void)
{
	/* The ACTIVE age, matching the branch that started this doze. Waking
	 * on the reading age instead would break the complementarity the grid
	 * in tests/sleep_gate pins, on exactly the desk this field exists
	 * for: a remembered reading never gets younger, so the board would
	 * doze on one number and refuse to wake on the other. */
	int32_t active_age = usage_freshness_active_age_s(k_uptime_get());

	return sleep_stale_should_wake(active_age, ota_blocks_sleep(),
				       usage_freshness_activity());
}

static void run_usb(void)
{
	int64_t last_tick = k_uptime_get();
	int stage_shown = 1;

	/* When the app was last heard from, for the waiting-for-host doze
	 * below. Starts at the top of the loop rather than at boot: this is
	 * about how long THIS screen has been unanswered. */
	int64_t host_quiet_since = last_tick;
	int64_t checking_since = 0;	/* OTA_UI_CHECKING entered at, ms */

	/* A tap that landed during boot, before this loop could act on it, is
	 * stale by now; ui_settings.h explains. Throw it away. */
	ui_settings_drop_pending();

	while (1) {
		proto_service();

		if (usage_view_have_data()) {
			ota_health = true;	/* daemon delivered usage */
		}
		ota_boot_pump();
		fatal_pump();

		/* Two reasons to doze (docs/sleep-mode-design.md). The first
		 * is silence past the host timeout. The second is an app that
		 * never stopped talking and has had nothing new to say for
		 * hours -- which is what a sleeping computer with a running
		 * daemon actually looks like from here, and why the first
		 * rule alone left a panel awake all night (field report
		 * 2026-09-02). Both block until there is a reason to wake.
		 *
		 * And neither of them while the owner is in the settings
		 * panel.
		 *
		 * Both rules below are about an absent person, and an open
		 * panel is a present one -- the same argument sleep_gate.c
		 * makes for refusing to doze over anything on screen that is
		 * asking for somebody. It also closes the loop ui_sleep.c
		 * opens at the other end: a swipe during a peek ends the
		 * doze, this loop opens the panel on its next pass, and
		 * without this the board would fall asleep again over the
		 * open panel sixty seconds later -- with the owner reading
		 * it. The cost is that a panel left open holds the board
		 * awake indefinitely; that is a deliberate act by a person
		 * standing at the desk, and it ends when they close it.
		 */
		if (!ui_settings_busy()) {
			/* Whether an update forbids dozing is one question
			 * with one answer, and it lives at
			 * ota_blocks_sleep() -- including the test-boot term,
			 * whose absence from the copy that used to sit here
			 * cost a board on the bench (2026-09-03). Taken once
			 * so both rules below judge the same instant. */
			bool ota_busy = ota_blocks_sleep();

			if (sleep_should_start(proto_host_lost(),
					       usage_view_have_data(),
					       ota_busy)) {
				ui_sleep_run(host_is_back,
					     "Your computer may be asleep.");
				continue;
			}
			/*
			 * On the ACTIVE age, never the reading's own.
			 *
			 * "Has this number stopped moving?" and "has this
			 * desk gone quiet?" are two questions, and the
			 * daemon answers both because they can differ: it
			 * re-offers the last five-hour reading it saw at its
			 * original time, so the dial can be twelve hours old
			 * five seconds after Claude Code rewrote the file it
			 * came from. Dozing on the first question closed the
			 * board's eyes on an owner who was sitting in front
			 * of it (field review 2026-09-02). ui_sleep.c's
			 * closing stamp keeps asking the first question,
			 * because that one really is about the number.
			 */
			if (sleep_stale_should_start(
				    usage_freshness_active_age_s(
					    k_uptime_get()),
				    usage_view_have_data(), ota_busy,
				    usage_freshness_activity())) {
				ui_sleep_run(reading_moved_again,
					     "No new readings for a while.");
				continue;
			}
		}

		/*
		 * OTA, via the daemon. The board has no network of its own, so
		 * ota_check()/ota_install() cannot run and the update row used
		 * to do nothing at all when tethered (user-reported
		 * 2026-08-20). The daemon has both an
		 * internet connection and this link, so it does the fetching and
		 * pushes the image down; see the OTA block in proto.c.
		 *
		 * Gated on the daemon TALKING, not on it having delivered
		 * usage. Those are unrelated: the usage endpoint is
		 * aggressively rate-limited (the bridge backs off 120-600 s on
		 * a 429), and gating here on ota_health meant a throttled
		 * session never checked for updates at all -- observed
		 * 2026-08-21. Nothing on the board -- no clock, no credential
		 * -- takes part; the fetch is entirely the daemon's.
		 */
		/* The boot-time check moved into proto.c's welcome handler,
		 * which fires on every host connection rather than the first;
		 * see there. */
		/*
		 * Both flags are taken unconditionally, before either is acted
		 * on. They are consume-on-read, so reading one only when the
		 * other is clear leaves a stale request armed for the next
		 * turn -- and reading them inside the call would leave the
		 * order to the compiler. upd_tap.h has the rule and the bug
		 * that produced it: a customer's tap on "Update now" during a
		 * daemon restart went nowhere at all.
		 */
		{
			bool inst_req = ota_take_install_request();
			bool chk_req = ota_take_check_request();

			switch (upd_tap_act(inst_req, chk_req,
					    proto_ota_staged())) {
			case UPD_ACT_CHECK:
				proto_ota_check();
				break;
			case UPD_ACT_INSTALL:
				proto_ota_install();
				break;
			case UPD_ACT_REFUSED:
				/* Consent with nothing to spend it on. Say so
				 * here rather than let the 30 s watchdog below
				 * describe it as a failed check: the customer
				 * asked to install, and an answer about
				 * checking sends them somewhere else. */
				ota_ui_set(OTA_UI_FAILED, NULL, 0);
				ota_ui_set_error("The app stopped answering");
				break;
			case UPD_ACT_NONE:
				break;
			}
		}
		/*
		 * A check nobody answers must not hang the row. The query goes
		 * out over the cable; if the daemon was restarting at that
		 * moment (an app update does exactly that) it was never read,
		 * and "Checking..." with the row disabled stayed on screen
		 * for good (user-reported 2026-08-29). Thirty seconds is far
		 * past a feed fetch; after it the row says why and a tap
		 * asks again.
		 */
		{
			struct ota_ui snap;

			ota_ui_get(&snap);
			if (snap.st == OTA_UI_CHECKING) {
				if (checking_since == 0) {
					checking_since = k_uptime_get();
				} else if (k_uptime_get() - checking_since
					   > 30 * 1000) {
					ota_ui_set(OTA_UI_FAILED, NULL, 0);
					ota_ui_set_error("No answer from the app");
					checking_since = 0;
				}
			} else {
				checking_since = 0;
			}
		}
		/* No completion handling: the daemon writes slot0 with esptool
		 * and resets the board itself, so the next thing that happens
		 * here is a boot. See the OTA block in proto.c.
		 */

		if (ui_anim_pending()) {
			ui_anim_run(usb_anim_pump);
		}
		ui_settings_service(usb_anim_pump);
		/* Not while Settings is up or asked for: the line is the gauge
		 * screen's, and do_open prints its own on the way in. */
		if (!ui_settings_busy()) {
			mem_stats_gauges_tick(usage_view_have_data());
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

			/*
			 * Waiting-for-host timeout.
			 *
			 * This used to cold-reboot into standalone
			 * (user request 2026-07-16), on the theory that a
			 * daemon which has not spoken is not coming back and
			 * a board with WiFi and a token can serve itself. On
			 * a desk it read as an unexplained reset, and it made
			 * the two never-heard-from-a-daemon cases behave
			 * differently for no reason a user could see: a board
			 * that once talked to the app dozes, a board that
			 * never did rebooted. Now both doze (owner's call,
			 * 2026-09-02). The cost is real and accepted: a board
			 * that could have fetched its own usage over WiFi
			 * sleeps instead. Since 2026-10-27 no board can fetch
			 * for itself, so nothing is given up any more.
			 *
			 * Still gated on the host being GONE rather than
			 * slow, which is what stops a live-but-busy daemon
			 * from dozing us mid-conversation -- and note that
			 * proto_host_seen() goes false again after
			 * HOST_TIMEOUT_MS, so the timer restarts on every
			 * silence rather than only the first.
			 */
			if (proto_host_seen()) {
				host_quiet_since = k_uptime_get();
			} else if (!ota_blocks_sleep() &&
				   !ui_settings_busy() &&
				   k_uptime_get() - host_quiet_since
				   > 60 * 1000) {
				/* Both refusals are the ones argued at the
				 * doze block above, and this is the branch
				 * they matter most in. It is the one that
				 * was found looping on the watchdog -- this
				 * loop feeds the unconfirmed image's, and a
				 * board with no daemon is exactly the board
				 * whose image cannot prove itself, so the
				 * two conditions coincide precisely here.
				 * And it is the one an owner meets when the
				 * app is not installed: this is the doze
				 * they swipe out of to reach settings, so
				 * dozing again while they are in there would
				 * shut the door behind them. The timer keeps
				 * running, so the doze happens the moment
				 * either reason clears. */
				printk("[usage] no app after 60 s; dozing until one speaks\n");
				ui_sleep_run(host_is_back,
					     "Waiting for the app on your computer.");
				host_quiet_since = k_uptime_get();
				continue;
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
	fatal_boot();	/* settle the fault-reboot streak; see fatal_policy.h */

	/* mbedTLS allocates from the kernel heap, not from libc. prj.conf turns
	 * MBEDTLS_ENABLE_HEAP off, so there is no static _mbedtls_heap, and the
	 * fallback would be picolibc's calloc with about 5 KB behind it.
	 *
	 * The reason this was first wired up is history. The WiFi build ran
	 * TLS handshakes whose 16 KB inbound record buffer (the release CDN
	 * sends full-size records) only fitted because TLS and the WiFi driver
	 * took turns in one HEAP_MEM_POOL_SIZE, sized for the bigger peak
	 * rather than both. That build is gone (2026-10-27). ota.c still links
	 * mbedTLS for its hashing, though, and pointing it at a heap that can
	 * hold it costs one call. k_calloc/k_free have exactly the signatures
	 * mbedTLS wants. */
	if (mbedtls_platform_set_calloc_free(k_calloc, k_free) != 0) {
		printk("[usage] FATAL: mbedTLS allocator hookup failed\n");
		return -1;
	}

	if (!device_is_ready(display_dev)) {
		printk("[usage] display not ready\n");
		return -1;
	}
	panel_fix_madctl();
	display_blanking_off(display_dev);
	ui_touchfx_init();	/* light touch-echo feedback on every press */

	cfg_init();
	/* The stored theme, before a single screen exists, so every screen is
	 * born in it and nothing has to be repainted at boot. */
	ui_theme_init();
	ota_boot_begin();	/* unconfirmed image? start the confirm clock */
	/* ...and from here the boot screen's own wait loops feed it too; they
	 * block for seconds against a 30 s window. */
	ui_boot_set_pump(ota_boot_pump);
	backlight_init();	/* drive the PWM to the persisted level */
	/* A refresh token left behind by an old WiFi build. This firmware has
	 * no use for one -- the sign-in code was deleted on 2026-10-27 after
	 * a long time compiled out -- yet deleting the code does not delete
	 * the token, and on an unfused board esptool reads the settings
	 * partition straight off the chip. So the first boot that finds one
	 * wipes it.
	 *
	 * Only the token. The network SSID and password, and the setup AP's
	 * password, stay in the record in plain text on such a board:
	 * cfg_clear_wifi() lost its last caller with the Settings reset rows,
	 * and adding one here would change a removal meant to leave the image
	 * byte-identical. firmware/README.md tells an owner how to erase them.
	 * The fields themselves stay, unread, so the record's layout holds. */
	{
		char tok[CFG_TOKEN_MAX];

		if (cfg_get_token(tok, sizeof(tok))) {
			cfg_clear_token();
			printk("[usage] cleared a stored refresh token: no WiFi in this build\n");
		}
	}

	/* proto first: its hello goes out now, so a daemon's reply is already
	 * in flight during the splash and the selection can short-circuit. */
	proto_init();

	ui_boot_splash();

	/* Has the daemon already answered? If the cable goes in later instead,
	 * nothing is lost: opening the port resets the board, and the hello
	 * goes out again into a new splash. */
	if (proto_host_seen()) {
		printk("[usage] mode: USB bridge\n");
		usage_view_init();
		/* Start the bar on step 2: the hello was answered while the
		 * splash was up. */
		usage_view_boot_begin(usb_boot_steps, 2);
		usage_view_boot_stage(1);
		lv_timer_handler();
		ui_boot_teardown();	/* only after the new screen is loaded */
		ui_settings_attach(lv_scr_act());
		ota_report_outcome();
		usage_view_set_status(USAGE_STATUS_DISCONNECTED);
		proto_resync();		/* daemon re-pushes time+usage right away */
		run_usb();
	}

	/*
	 * Nobody has answered yet. This used to be a three-way choice (the
	 * daemon, a saved home network, the setup screen); the WiFi mode's
	 * removal on 2026-10-27 left only the first, and run_usb() handles a
	 * daemon that is still to come just as well: it shows the
	 * waiting-for-host step and times out on its own if no daemon ever
	 * speaks. Deliberately no proto_resync() here -- that exists to make a
	 * daemon that already said hello re-push, and by definition none has.
	 */
	printk("[usage] mode: USB bridge (no host yet)\n");
	usage_view_init();
	usage_view_boot_begin(usb_boot_steps, 2);
	lv_timer_handler();
	ui_boot_teardown();
	ui_settings_attach(lv_scr_act());
	ota_report_outcome();
	usage_view_set_status(USAGE_STATUS_DISCONNECTED);
	run_usb();
	return 0;
}
