#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <lvgl.h>
#include <string.h>

#include "ui_theme.h"
#include "cfg_store.h"
#include "proto.h"
#include "ui_settings.h"
#include "usage_view.h"

/*
 * A preview reverts by itself after 15 minutes without another theme line.
 *
 * The builder sends one on every change and the daemon relays them, so a
 * live session keeps resetting this. What it catches is the session that
 * never ends properly: the laptop lid shut on the builder, the daemon
 * restarted mid-edit. Without it the board would wear an unsaved half-made
 * theme until the next reboot, and a reboot is the one fix nobody thinks of
 * for "my colours changed by themselves". (Orch contract 2026-10-27.)
 */
#define PREVIEW_MS (15 * 60 * 1000)

static lv_timer_t *preview_timer;	/* non-NULL while a preview is up */

static void to_custom(const struct cfg_theme *r, struct theme_custom *t)
{
	for (int i = 0; i < 7; i++) {
		t->c[i] = ((uint32_t)r->rgb[3 * i] << 16) |
			  ((uint32_t)r->rgb[3 * i + 1] << 8) | r->rgb[3 * i + 2];
	}
	t->w = r->w;
	t->flat = r->ends == 1;
	t->dot = r->dot == 1;
	t->by_gauge = r->by == 1;
}

static void from_custom(const struct theme_custom *t, struct cfg_theme *r)
{
	for (int i = 0; i < 7; i++) {
		r->rgb[3 * i] = (uint8_t)(t->c[i] >> 16);
		r->rgb[3 * i + 1] = (uint8_t)(t->c[i] >> 8);
		r->rgb[3 * i + 2] = (uint8_t)t->c[i];
	}
	r->w = t->w;
	r->ends = t->flat ? 1 : 0;
	r->dot = t->dot ? 1 : 0;
	r->by = t->by_gauge ? 1 : 0;
}

bool ui_theme_custom_stored(struct theme_custom *out)
{
	struct cfg_theme r;

	cfg_get_theme(&r);
	to_custom(&r, out);
	/* An unset record is all zeros -- width 0 and black on black -- so
	 * "never saved" and "saved something unusable" are both just
	 * invalid, and neither is offered in the list. */
	return theme_custom_valid(out);
}

int ui_theme_stored_id(void)
{
	struct cfg_theme r;
	struct theme_custom t;

	cfg_get_theme(&r);
	if (r.id == THEME_CUSTOM) {
		return ui_theme_custom_stored(&t) ? THEME_CUSTOM : THEME_DARK;
	}
	return r.id < THEME_BUILTIN_N ? r.id : THEME_DARK;
}

/*
 * Show the stored theme. Anything it cannot honour -- an id from a future
 * firmware after a downgrade, a custom theme whose bytes no longer validate
 * -- falls back to Dark rather than to whatever was showing, because Dark is
 * the one theme every board is guaranteed to draw well.
 */
static void select_stored(void)
{
	int id = ui_theme_stored_id();
	struct theme_custom t;

	if (id == THEME_CUSTOM && ui_theme_custom_stored(&t) &&
	    theme_select_custom(&t)) {
		return;
	}
	if (!theme_select(id)) {
		theme_select(THEME_DARK);
	}
}

static void repaint(void)
{
	usage_view_apply_theme();
	ui_settings_apply_theme();
}

static void end_preview(void)
{
	if (preview_timer) {
		lv_timer_delete(preview_timer);
		preview_timer = NULL;
	}
}

static void preview_expired(lv_timer_t *t)
{
	ARG_UNUSED(t);
	/* Repeat count 1: LVGL deletes the timer itself after this returns. */
	preview_timer = NULL;
	printk("[theme] preview timed out; back to the stored theme\n");
	select_stored();
	repaint();
}

void ui_theme_init(void)
{
	select_stored();
}

bool ui_theme_previewing(void)
{
	return preview_timer != NULL;
}

static bool show(int id, const struct theme_custom *t)
{
	return id == THEME_CUSTOM ? theme_select_custom(t) : theme_select(id);
}

/*
 * True only when the theme is on flash. Every caller tells the host on true
 * and on nothing else: theme_now names the STORED id, so a "saved" or "user"
 * sent after a failed store would vouch for a theme the board does not hold.
 * Until the final review of the theme work (N1, 2026-10-27) this returned
 * nothing, and a failed store was logged and then answered why=saved; the
 * daemon recorded it in theme_sent.json and never sent the save again.
 * tests/ui_theme holds the callers to this.
 */
static bool store(int id, const struct theme_custom *t)
{
	struct cfg_theme r;

	/* Read-modify-write: choosing a built-in keeps the custom bytes, so
	 * the custom theme stays in the list for the next time. */
	cfg_get_theme(&r);
	r.id = (uint8_t)id;
	if (id == THEME_CUSTOM) {
		from_custom(t, &r);
	}
	if (cfg_set_theme(&r) != 0) {
		printk("[theme] could not store the theme\n");
		return false;
	}
	return true;
}

/*
 * A store failed: show what the board really holds again, and tell the host
 * nothing. The contract has no word for "could not store", and the two it
 * does have both mislead here. "saved" is the lie being removed. "boot"
 * would make the daemon ask again on its next fast tick (FAST_POLL_INTERVAL_S,
 * 2 s) -- an erase attempt every two seconds on a flash that has just
 * refused one. Saying nothing leaves the daemon's `asked` holding the save
 * until it next greets the board -- a board reboot, or the daemon
 * reconnecting. The board answers that welcome with why=boot and the id it
 * really stored (cfg_store.c puts its RAM record back on a failed write, so
 * that is the old one), the daemon sees it differs from the saved theme, and
 * the save goes out once more. theme_sent.json keeps the last theme the
 * board truly confirmed. Showing the stored theme keeps the screen,
 * theme_now and the next boot all in agreement; leaving the new one up would
 * be colours that a reboot takes away, the very surprise PREVIEW_MS exists
 * to prevent.
 */
static void store_failed(void)
{
	select_stored();
	repaint();
}

bool ui_theme_choose(int id)
{
	struct theme_custom t = { 0 };

	if (id == THEME_CUSTOM && !ui_theme_custom_stored(&t)) {
		return false;
	}
	if (!show(id, &t)) {
		return false;
	}
	end_preview();
	if (!store(id, &t)) {
		store_failed();
		return false;
	}
	repaint();
	proto_send_theme_now("user");
	return true;
}

void ui_theme_wire(const struct theme_msg *m)
{
	switch (m->op) {
	case THEME_OP_CANCEL:
		if (preview_timer == NULL) {
			return;		/* nothing to go back from */
		}
		end_preview();
		select_stored();
		repaint();
		printk("[theme] preview cancelled\n");
		return;
	case THEME_OP_PREVIEW:
		if (!show(m->id, &m->custom)) {
			return;
		}
		if (preview_timer) {
			lv_timer_reset(preview_timer);
		} else {
			preview_timer = lv_timer_create(preview_expired,
							PREVIEW_MS, NULL);
			if (preview_timer == NULL) {
				/* Out of pool, so nothing would ever take
				 * this preview down again: show the stored
				 * theme rather than an unsaved one forever. */
				select_stored();
				repaint();
				return;
			}
			lv_timer_set_repeat_count(preview_timer, 1);
		}
		repaint();
		return;
	case THEME_OP_SAVE:
		if (!show(m->id, &m->custom)) {
			return;
		}
		end_preview();
		if (!store(m->id, &m->custom)) {
			store_failed();
			return;
		}
		repaint();
		proto_send_theme_now("saved");
		printk("[theme] saved\n");
		return;
	}
}
