/* When the board says a theme is stored, it is.
 *
 * Build & run (tests/ci/check_host_tests.sh does this):
 *   cc -I firmware/src -I tests/ui_theme/stub -I tests/cfg_store/stub \
 *      tests/ui_theme/host_test.c firmware/src/ui_theme.c \
 *      firmware/src/cfg_store.c firmware/src/theme.c firmware/src/msg_parse.c \
 *      -lm -o /tmp/ui_theme && /tmp/ui_theme
 *
 * The REAL ui_theme.c, cfg_store.c and theme.c, over tests/cfg_store's RAM
 * flash with its erases made to fail. Until this file, ui_theme.c had no
 * test at all, and the final review of the theme work (N1, 2026-10-27) found
 * what that hid: a failed cfg_set_theme() was logged and then answered with
 * theme_now why=saved anyway. The daemon takes "saved" as the board's word --
 * it writes theme_sent.json and stops sending the save -- so the computer
 * believed the board held a theme it did not, and the board went on wearing
 * colours that a reboot would quietly take away.
 *
 * The contract has no word for "could not store", and this does not invent
 * one. On a failed store the board sends NOTHING and goes back to the theme
 * it really holds:
 *   - No "saved": theme_sent.json keeps the last theme truly confirmed.
 *   - No "boot" either, though the daemon would read it as "ask again": it
 *     would ask on its next fast tick, every two seconds, and a store that
 *     failed once on this flash is an erase attempt every two seconds against
 *     flash already misbehaving. With nothing sent, the daemon's `asked`
 *     holds the save until it next greets the board (a board reboot, or the
 *     daemon reconnecting). The board answers that welcome with why=boot and
 *     the id it really stored, which differs from the saved one, so the save
 *     goes out once more.
 *   - No "user" from Settings: theme_now reports the STORED id, so a "user"
 *     after a failed store would name the old theme as the owner's choice,
 *     and the daemon would adopt it over the one saved on the computer.
 *   - The screen shows the stored theme again, so what the owner sees is what
 *     theme_now reports and what the next boot will show.
 *   - A retry once the flash takes writes again really writes. That needs
 *     cfg_store.c to have put its RAM record back on the failure: if it kept
 *     the unstored values, its "nothing changed" check skipped the retry and
 *     reported success (review of 1cb4fd6, M1).
 */
#include <stdio.h>
#include <string.h>

#include <lvgl.h>
#include <zephyr/storage/flash_map.h>

#include "cfg_store.h"
#include "theme.h"
#include "ui_theme.h"

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

/*
 * ---- the REAL cfg_store.c, over a RAM flash that can refuse erases ----
 *
 * The first version of this file faked cfg_set_theme(), and the fake
 * committed only on success -- kinder than the real function, which puts the
 * new values into its RAM record first and then writes. So it passed a fix
 * that did not work on a board: after a failed store the "revert" showed the
 * theme that had just failed to store, and the next hello reported it as
 * held (review of 1cb4fd6, M1). Linking the real store means the two files
 * are tested together, as they run.
 */
uint8_t fake_flash[FAKE_FLASH_SIZE];
long fake_write_budget = -1;
int fake_writes;
int fake_erase_rc;

static int stored_id(void)
{
	struct cfg_theme r;

	cfg_get_theme(&r);
	return r.id;
}

/* ---- what the host hears ---- */
static char sent[8][16];
static int n_sent;

void proto_send_theme_now(const char *why)
{
	if (n_sent < 8) {
		snprintf(sent[n_sent], sizeof(sent[0]), "%s", why);
	}
	n_sent++;
}

static int sent_count(const char *why)
{
	int n = 0;

	for (int i = 0; i < n_sent && i < 8; i++) {
		n += strcmp(sent[i], why) == 0;
	}
	return n;
}

/* ---- the screens ---- */
static int repaints;

void usage_view_apply_theme(void)
{
	repaints++;
}

void ui_settings_apply_theme(void)
{
}

/* ---- one LVGL timer, which is all ui_theme.c ever holds ---- */
struct lv_timer { int alive; };
static struct lv_timer the_timer;

lv_timer_t *lv_timer_create(lv_timer_cb_t cb, uint32_t period, void *user)
{
	(void)cb;
	(void)period;
	(void)user;
	the_timer.alive = 1;
	return &the_timer;
}

void lv_timer_delete(lv_timer_t *t)
{
	t->alive = 0;
}

void lv_timer_reset(lv_timer_t *t)
{
	(void)t;
}

void lv_timer_set_repeat_count(lv_timer_t *t, int32_t n)
{
	(void)t;
	(void)n;
}

static void fresh(int id)
{
	struct cfg_theme r;

	memset(fake_flash, 0xFF, sizeof(fake_flash));
	fake_erase_rc = 0;
	cfg_init();
	cfg_get_theme(&r);
	r.id = (uint8_t)id;
	cfg_set_theme(&r);
	n_sent = 0;
	repaints = 0;
	the_timer.alive = 0;
	ui_theme_init();
}

/* The flash stops taking erases, and every store from here on fails. */
static void flash_fails(void)
{
	fake_erase_rc = -5;	/* -EIO */
}

static struct theme_msg msg(enum theme_op op, int id)
{
	struct theme_msg m;

	memset(&m, 0, sizeof(m));
	m.op = op;
	m.id = id;
	return m;
}

int main(void)
{
	struct theme_msg m;

	/* --- the PC saves, and the store takes it --- */
	fresh(THEME_DARK);
	m = msg(THEME_OP_SAVE, 9);
	ui_theme_wire(&m);
	CHECK(stored_id() == 9, "save: stored");
	CHECK(n_sent == 1 && sent_count("saved") == 1,
	      "save: the board answers why=saved");
	CHECK(theme_active_id() == 9, "save: the saved theme is showing");

	/* --- the PC saves, and the store fails --- */
	fresh(THEME_DARK);
	flash_fails();
	m = msg(THEME_OP_SAVE, 9);
	ui_theme_wire(&m);
	CHECK(sent_count("saved") == 0,
	      "failed save: the board does not claim it saved");
	CHECK(n_sent == 0,
	      "failed save: nothing at all is sent, so the daemon does not"
	      " re-ask every tick");
	CHECK(stored_id() == THEME_DARK && ui_theme_stored_id() == THEME_DARK,
	      "failed save: the stored theme is the old one");
	CHECK(theme_active_id() == THEME_DARK && repaints > 0,
	      "failed save: the screen goes back to the stored theme");

	/* --- the flash recovers, and the same save comes again (the daemon's
	 * retry after its next greeting) --- */
	fake_erase_rc = 0;
	n_sent = 0;
	{
		int before = fake_writes;

		m = msg(THEME_OP_SAVE, 9);
		ui_theme_wire(&m);
		CHECK(fake_writes > before && stored_id() == 9,
		      "retried save: it is really written, not skipped as"
		      " already stored");
	}
	CHECK(sent_count("saved") == 1 && theme_active_id() == 9,
	      "retried save: confirmed, and showing");
	cfg_init();
	CHECK(stored_id() == 9, "retried save: still there after a reboot");

	/* --- the same, with a preview up first --- */
	fresh(THEME_DARK);
	m = msg(THEME_OP_PREVIEW, 4);
	ui_theme_wire(&m);
	CHECK(theme_active_id() == 4 && the_timer.alive,
	      "preview: shown, with its timeout running");
	flash_fails();
	m = msg(THEME_OP_SAVE, 4);
	ui_theme_wire(&m);
	CHECK(n_sent == 0 && theme_active_id() == THEME_DARK,
	      "failed save after a preview: nothing sent, stored theme shown");
	CHECK(!the_timer.alive,
	      "failed save after a preview: the preview is over, not left to"
	      " time out later");

	/* --- a preview is visible to Settings, and choosing ends it --- */
	fresh(THEME_DARK);
	CHECK(!ui_theme_previewing(), "previewing: not before a preview");
	m = msg(THEME_OP_PREVIEW, 4);
	ui_theme_wire(&m);
	CHECK(ui_theme_previewing(), "previewing: while one is up");
	CHECK(ui_theme_choose(THEME_DARK) && !ui_theme_previewing() &&
	      theme_active_id() == THEME_DARK && stored_id() == THEME_DARK,
	      "choosing the stored theme during a preview takes it down");
	m = msg(THEME_OP_PREVIEW, 4);
	ui_theme_wire(&m);
	m = msg(THEME_OP_CANCEL, 0);
	ui_theme_wire(&m);
	CHECK(!ui_theme_previewing(), "previewing: not after a cancel");

	/* --- the owner picks in Settings --- */
	fresh(THEME_DARK);
	CHECK(ui_theme_choose(9) && stored_id() == 9 &&
	      sent_count("user") == 1 && theme_active_id() == 9,
	      "choose: stored, shown, and reported as the owner's choice");

	fresh(THEME_DARK);
	flash_fails();
	CHECK(!ui_theme_choose(9), "failed choose: reported as not done");
	CHECK(n_sent == 0,
	      "failed choose: no why=user naming the old theme as a choice");
	CHECK(theme_active_id() == THEME_DARK && stored_id() == THEME_DARK,
	      "failed choose: the stored theme is still the one showing");
	fake_erase_rc = 0;
	n_sent = 0;
	{
		int before = fake_writes;

		CHECK(ui_theme_choose(9) && fake_writes > before &&
		      sent_count("user") == 1,
		      "failed choose, tapped again: written and reported this"
		      " time");
	}
	cfg_init();
	CHECK(stored_id() == 9, "failed choose, tapped again: kept by a reboot");

	printf(failures ? "\n%d FAILED\n" : "\nall ui_theme checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
