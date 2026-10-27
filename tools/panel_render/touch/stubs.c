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

/* --- OTA: always idle ------------------------------------------------------ */

void ota_ui_get(struct ota_ui *out)
{
	memset(out, 0, sizeof(*out));
	out->st = OTA_UI_IDLE;
}

enum ota_source ota_ui_source(void)
{
	return OTA_SRC_USB;
}

void ota_ui_set_source(enum ota_source src)
{
	(void)src;
}

void ota_request_check(void)
{
}

void ota_request_install(void)
{
}

void ota_ui_set(enum ota_ui_state st, const struct ota_manifest *m, uint8_t pct)
{
	(void)st;
	(void)m;
	(void)pct;
}

void ota_ui_set_error(const char *why)
{
	(void)why;
}

bool ota_badge(void)
{
	return false;
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
	return "";
}

bool proto_ota_install(void)
{
	return false;
}

bool proto_ota_staged(void)
{
	return false;
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
