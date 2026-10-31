/*
 * Everything ui_settings.c, ui_theme.c and usage_view.c reach for that is
 * not the screens themselves: storage, the backlight, OTA, the boot clip and
 * the protocol. Each holds just enough state to answer the way the board
 * would -- a stored theme reads back, a brightness level sticks -- and the
 * protocol's two outbound lines the bench listens for (theme_now, theme_edit)
 * go to stdout as the board sends them.
 */
#include <stdio.h>
#include <string.h>

#include "cfg_store.h"
#include "backlight.h"
#include "ota.h"
#include "proto.h"
#include "ui_boot.h"
#include "ui_theme.h"
#include "theme.h"
#include "version.h"
#include "ui_settings.h"
#include "harness.h"

/* --- cfg_store, in RAM -------------------------------------------------- */

static struct cfg_theme theme_rec;
static uint8_t focus_on, focus_win;

void cfg_get_theme(struct cfg_theme *out)
{
	*out = theme_rec;
}

int cfg_set_theme(const struct cfg_theme *t)
{
	theme_rec = *t;
	return 0;
}

uint8_t cfg_get_focus_on(void)
{
	return focus_on;
}

uint8_t cfg_get_focus_win(void)
{
	return focus_win;
}

int cfg_set_focus(uint8_t on, uint8_t win)
{
	focus_on = on;
	focus_win = win;
	return 0;
}

uint8_t cfg_get_main_src(void)
{
	return CFG_MAIN_SRC_CLAUDE;
}

uint8_t cfg_get_ota_state(char *ver, size_t len)
{
	if (len) {
		ver[0] = '\0';
	}
	return 0;
}

int cfg_set_ota_state(uint8_t st, const char *ver)
{
	(void)st;
	(void)ver;
	return 0;
}

/* --- the backlight --------------------------------------------------------- */

static uint8_t bright = 60;

void backlight_set(uint8_t pct)
{
	bright = pct;
}

uint8_t backlight_get(void)
{
	return bright;
}

void backlight_step(int dir)
{
	bright = backlight_next_level(bright, dir);
}

/* --- OTA: the state ota.c keeps, moved the way proto.c and main.c move it --
 *
 * So the popups only an update can raise -- the offer, the install confirm,
 * the download screen, the failure notice -- come up here from the same wire
 * lines a board gets: harness.c hands ota_avail, ota_none and ota_error to
 * the three harness_ota_* below, as proto.c's dispatch does. A tap on
 * "Update now" or "Yes, do it" says ota_flash on stdout, as
 * proto_ota_install() does, and nothing else: the bench never answers it,
 * and there is no flash here to write. */

static struct ota_ui ota_now = { .st = OTA_UI_IDLE };
static bool ota_staged_now;
static char ota_app_now[16];
static bool ota_badge_now;

void ota_ui_get(struct ota_ui *out)
{
	*out = ota_now;
}

enum ota_source ota_ui_source(void)
{
	return OTA_SRC_USB;
}

void ota_ui_set_source(enum ota_source src)
{
	(void)src;
}

/* ota.c's ota_ui_set, without the lock. */
void ota_ui_set(enum ota_ui_state st, const struct ota_manifest *m, uint8_t pct)
{
	ota_now.st = st;
	if (m) {
		snprintf(ota_now.version, sizeof(ota_now.version), "%s",
			 m->version);
		ota_now.size = m->size;
	}
	ota_now.pct = pct;
	if (st != OTA_UI_FAILED) {
		ota_now.err[0] = '\0';
	}
	if (st == OTA_UI_AVAILABLE) {
		ota_badge_now = true;
	} else if (st == OTA_UI_UP_TO_DATE || st == OTA_UI_REBOOTING) {
		ota_badge_now = false;
	}
}

void ota_ui_set_error(const char *why)
{
	snprintf(ota_now.err, sizeof(ota_now.err), "%s", why ? why : "");
}

bool ota_badge(void)
{
	return ota_badge_now;
}

/* main.c's handling of the two requests, at once instead of on its next
 * pass: upd_tap_act(), then proto_ota_check() or proto_ota_install(). */
/*
 * A check is also made visible. On a board it shows as the tile reading
 * "Checking..." and comes back from the daemon as a state change; the
 * bench answers this ota_query with ota_none, and UP_TO_DATE raises no
 * notice, so on its own the check would leave nothing `expect` can see.
 * The request therefore also puts up a notice -- of kind "ota", the one an
 * update's outcome uses -- which the bench sees as modal=notice and nt=ota.
 * That is what lets tools/bench/full_touch.txt say a swipe on the Update
 * tile started no check: without it, the line could not fail. No shipped
 * script taps the tile to check on purpose; one that does will find this
 * notice up and must dismiss it.
 */
void ota_request_check(void)
{
	ota_staged_now = false;
	ota_app_now[0] = '\0';
	ota_ui_set(OTA_UI_CHECKING, NULL, 0);
	harness_emit("{\"t\":\"ota_query\",\"v\":2,\"cur\":\"" BLINK_FW_VERSION "\"}");
	ui_settings_notice("Update check requested");
	ui_settings_bench_notice_is("ota");
}

void ota_request_install(void)
{
	if (!ota_staged_now) {
		ota_ui_set(OTA_UI_FAILED, NULL, 0);
		/* main.c's UPD_ACT_REFUSED wording, copied: main.c is not
		 * compiled here, and tests/test_board_touch_widgets.py holds the
		 * two to the same sentence. */
		ota_ui_set_error("The app stopped answering");
		return;
	}
	ota_ui_set(OTA_UI_DOWNLOADING, NULL, 0);
	harness_emit("{\"t\":\"ota_flash\",\"v\":2}");
}

/* proto.c's dispatch of the three answers. */
void harness_ota_avail(const char *version, uint32_t size, const char *app)
{
	struct ota_manifest m = { 0 };

	snprintf(m.version, sizeof(m.version), "%s", version);
	m.size = size;
	snprintf(ota_app_now, sizeof(ota_app_now), "%s", app ? app : "");
	ota_staged_now = true;
	ota_ui_set(OTA_UI_AVAILABLE, &m, 0);
}

void harness_ota_none(void)
{
	ota_staged_now = false;
	ota_app_now[0] = '\0';
	ota_ui_set(OTA_UI_UP_TO_DATE, NULL, 0);
}

void harness_ota_error(const char *why)
{
	ota_staged_now = false;
	ota_ui_set(OTA_UI_FAILED, NULL, 0);
	if (why && why[0]) {
		ota_ui_set_error(why);
	}
}

/* --- the protocol ---------------------------------------------------------- */

const char *proto_host_version(void)
{
	return "1.3.4";
}

bool proto_host_outdated(void)
{
	return false;
}

bool proto_host_can_theme(void)
{
	return true;		/* the bench's welcome lists "theme" */
}

void proto_ota_check(void)
{
}

const char *proto_ota_app_version(void)
{
	return ota_app_now;
}

bool proto_ota_install(void)
{
	return false;
}

bool proto_ota_staged(void)
{
	return ota_staged_now;
}

void harness_theme_now(const char *why)
{
	char buf[96];

	snprintf(buf, sizeof(buf),
		 "{\"t\":\"theme_now\",\"v\":2,\"id\":%d,\"why\":\"%s\"}",
		 ui_theme_stored_id(), why);
	harness_emit(buf);
}

void proto_send_theme_now(const char *why)
{
	harness_theme_now(why);
}

void proto_send_theme_edit(void)
{
	harness_emit("{\"t\":\"theme_edit\",\"v\":2}");
}

/* --- the boot screen ------------------------------------------------------- */

void ui_boot_mark_intentional_reboot(void)
{
}
