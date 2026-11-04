/*
 * The widget walk. touch_widgets.h has what it is for; touch_widgets_fmt.h
 * has the line it writes.
 *
 * WHAT IS LISTED. Every object with LV_OBJ_FLAG_CLICKABLE that is not hidden
 * (itself or by an ancestor) and has some part inside the panel once each
 * ancestor's clipping is applied -- the same three things that decide
 * whether lv_indev_search_obj() can ever return it. That includes LVGL's
 * plain containers, which are born clickable: a notice's box is one, and it
 * is listed with the notice's sentence as its label, which is how a bench
 * reads that a notice says what it should. It also includes the active
 * screen itself, as "screen", because the doze's screen is the control that
 * starts a sleep peek (ui_sleep.c, tap_cb).
 *
 * The rectangle is the object's drawn coords, clipped. It leaves out an
 * extended click area (the back button's and the theme pager's 10 px) and
 * any style transform, so a finger can land a little outside it and still
 * hit. The aim point does not have that problem: it is checked by the real
 * search, whatever the rectangle says.
 *
 * THE INDEX is the object's place in one fixed walk: lv_layer_sys, then
 * lv_layer_top, then the active screen, then the bottom layer, each depth
 * first in child order. It means nothing on its own. It is only good with
 * the generation it was listed under.
 *
 * THE GENERATION is a hash of everything the listing says about every
 * object except where to aim: its address, its visible rectangle, its kind,
 * its state flags, its label's bytes. A counter bumped wherever an object is
 * created or deleted would need a hook in LVGL; this changes exactly when
 * the listing would, which is what "stale" has to mean -- index 7 of the
 * Settings panel is not index 7 of the theme screen that replaced it. LVGL
 * reuses a freed block for the next object of the same size, so a popup
 * deleted and rebuilt identically in the same place can hash the same; a
 * click on it then lands on a control that is the same in every way a
 * finger could tell, which is the right thing to press.
 */
#include <string.h>
#include <lvgl.h>

#include "msg_parse.h"
#include "touch_widgets.h"
#include "touch_widgets_fmt.h"

#define TW_LAYERS 4

struct walk {
	uint32_t gen;
	int n;			/* entries seen so far */
	/* listing: */
	char *buf;
	size_t len;
	size_t at;
	int from, to;		/* the page: entries [from, to) are written */
	bool fits;
	/* aiming: */
	int want;
	bool found;
	bool disabled;
	enum tw_aim aim;
	int ax, ay;
};

static lv_obj_t *layer(int i)
{
	switch (i) {
	case 0:
		return lv_layer_sys();
	case 1:
		return lv_layer_top();
	case 2:
		return lv_screen_active();
	default:
		return lv_layer_bottom();
	}
}

/* lv_indev.c's pointer_search_obj, which is static there: the four layers,
 * top-most first, and the first that has something at the point wins. */
static lv_obj_t *hit_at(int x, int y)
{
	for (int i = 0; i < TW_LAYERS; i++) {
		lv_obj_t *root = layer(i);
		lv_point_t p = { x, y };
		lv_obj_t *o;

		if (root == NULL) {
			continue;
		}
		o = lv_indev_search_obj(root, &p);
		if (o) {
			return o;
		}
	}
	return NULL;
}

static bool aim_at(lv_obj_t *obj, const lv_area_t *vis, int *x, int *y)
{
	for (int k = 0; k < TW_PROBE_N; k++) {
		int px, py;

		tw_probe(k, vis->x1, vis->y1, vis->x2, vis->y2, &px, &py);
		if (hit_at(px, py) == obj) {
			*x = px;
			*y = py;
			return true;
		}
	}
	return false;
}

static bool clip(lv_area_t *out, const lv_area_t *a, const lv_area_t *b)
{
	out->x1 = LV_MAX(a->x1, b->x1);
	out->y1 = LV_MAX(a->y1, b->y1);
	out->x2 = LV_MIN(a->x2, b->x2);
	out->y2 = LV_MIN(a->y2, b->y2);
	return out->x1 <= out->x2 && out->y1 <= out->y2;
}

static int kind_of(lv_obj_t *obj, bool root)
{
	if (root) {
		return obj == lv_screen_active() ? TW_K_SCREEN : TW_K_LAYER;
	}
#if LV_USE_LIST
	if (lv_obj_has_class(obj, &lv_list_button_class)) {
		return TW_K_LIST;
	}
#endif
#if LV_USE_SWITCH
	if (lv_obj_has_class(obj, &lv_switch_class)) {
		return TW_K_SWITCH;
	}
#endif
#if LV_USE_SLIDER
	if (lv_obj_has_class(obj, &lv_slider_class)) {
		return TW_K_SLIDER;
	}
#endif
#if LV_USE_CHECKBOX
	if (lv_obj_has_class(obj, &lv_checkbox_class)) {
		return TW_K_CHECK;
	}
#endif
	if (lv_obj_has_class(obj, &lv_button_class)) {
		return TW_K_BUTTON;
	}
	return TW_K_OBJ;
}

/* The first direct child that is a label: a button's own words, a row's
 * title, a notice's sentence. Direct only, so a row's subtitle (its second
 * label) never stands in for its title. */
static const char *label_of(lv_obj_t *obj)
{
	uint32_t n = lv_obj_get_child_count(obj);

	for (uint32_t i = 0; i < n; i++) {
		lv_obj_t *c = lv_obj_get_child(obj, (int32_t)i);

		if (lv_obj_check_type(c, &lv_label_class)) {
			return lv_label_get_text(c);
		}
	}
	return "";
}

static void entry(struct walk *w, lv_obj_t *obj, const lv_area_t *vis,
		  int li, bool root)
{
	int idx = w->n++;
	int kind = kind_of(obj, root);
	const char *text = label_of(obj);
	size_t tl = strlen(text);
	int flags = 0;
	uintptr_t addr = (uintptr_t)obj;

	if (lv_obj_has_state(obj, LV_STATE_CHECKED)) {
		flags |= TW_F_CHECKED;
	}
	if (lv_obj_has_state(obj, LV_STATE_DISABLED)) {
		flags |= TW_F_DISABLED;
	}
	if (lv_obj_get_event_count(obj) > 0) {
		flags |= TW_F_EVENTS;
	}
	if (li == 1) {
		flags |= TW_F_TOP;
	} else if (li != 2) {
		flags |= TW_F_SYS;
	}

	w->gen = tw_fnv_int(w->gen, idx);
	w->gen = tw_fnv(w->gen, &addr, sizeof(addr));
	w->gen = tw_fnv_int(w->gen, vis->x1);
	w->gen = tw_fnv_int(w->gen, vis->y1);
	w->gen = tw_fnv_int(w->gen, vis->x2);
	w->gen = tw_fnv_int(w->gen, vis->y2);
	w->gen = tw_fnv_int(w->gen, kind);
	w->gen = tw_fnv_int(w->gen, flags);
	w->gen = tw_fnv(w->gen, text, tl < TW_LABEL_MAX ? tl : TW_LABEL_MAX);

	if (idx == w->want && w->want >= 0) {
		w->found = true;
		/* LVGL drops input to a disabled object even when it wins the
		 * hit test, so a press there would report done and do nothing
		 * (review of the walk, S4). Said, rather than played. */
		w->disabled = (flags & TW_F_DISABLED) != 0;
		w->aim = aim_at(obj, vis, &w->ax, &w->ay) ? TW_AIM_OK
							   : TW_AIM_COVERED;
	}
	if (w->buf && idx >= w->from && idx < w->to && w->fits) {
		char label[TW_LABEL_MAX + 1];
		int ax = -1, ay = -1;

		if (aim_at(obj, vis, &ax, &ay)) {
			flags |= TW_F_HIT;
		} else {
			ax = ay = -1;
		}
		tw_label_copy(label, sizeof(label), text);
		w->fits = tw_entry_put(w->buf, w->len, &w->at, idx == w->from,
				       idx, kind, label, vis->x1, vis->y1,
				       vis->x2 - vis->x1 + 1,
				       vis->y2 - vis->y1 + 1, ax, ay, flags);
	}
}

/*
 * One object and everything under it. `clip_in` is what its parent lets
 * show; an object that does not set OVERFLOW_VISIBLE clips its children to
 * itself, the way the hit test refuses to look into a child outside its
 * parent (lv_indev_search_obj).
 */
static void visit(struct walk *w, lv_obj_t *obj, const lv_area_t *clip_in,
		  int li, bool root)
{
	lv_area_t a, vis;
	bool shows;
	const lv_area_t *inner;
	uint32_t n;

	if (lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) {
		return;
	}
	lv_obj_get_coords(obj, &a);
	shows = clip(&vis, &a, clip_in);
	if (shows && lv_obj_has_flag(obj, LV_OBJ_FLAG_CLICKABLE)) {
		entry(w, obj, &vis, li, root);
	}
	if (lv_obj_has_flag(obj, LV_OBJ_FLAG_OVERFLOW_VISIBLE)) {
		inner = clip_in;
	} else if (shows) {
		inner = &vis;
	} else {
		return;
	}
	n = lv_obj_get_child_count(obj);
	for (uint32_t i = 0; i < n; i++) {
		visit(w, lv_obj_get_child(obj, (int32_t)i), inner, li, false);
	}
}

static void walk(struct walk *w)
{
	lv_area_t panel = {
		0, 0,
		lv_display_get_horizontal_resolution(NULL) - 1,
		lv_display_get_vertical_resolution(NULL) - 1,
	};

	w->gen = TW_FNV_INIT;
	w->n = 0;
	/*
	 * Layout first. A popup put up by a protocol line is placed by
	 * lv_obj_center() and flex, and its coordinates are the ones from
	 * before that until the display's next refresh runs the layout. Seen
	 * on the host harness: a listing taken in the same pass as the update
	 * notice put "What's new" at 78,94, where the box had been before it
	 * was centred, and the click pressed the notice's summary instead. The
	 * finger lands at least a report later, after that refresh, so the
	 * listing has to describe the panel as laid out. This is the work the
	 * refresh would do anyway, done a few milliseconds sooner.
	 */
	for (int i = 0; i < TW_LAYERS; i++) {
		lv_obj_t *root = layer(i);

		if (root) {
			lv_obj_update_layout(root);
		}
	}
	for (int i = 0; i < TW_LAYERS; i++) {
		lv_obj_t *root = layer(i);

		if (root) {
			visit(w, root, &panel, i, true);
		}
	}
}

int tw_page_line(char *buf, size_t len, int proto_v, int page)
{
	struct walk w = { 0 };

	if (page < 0) {
		page = 0;
	}
	w.buf = buf;
	w.len = len;
	w.want = -1;
	w.from = page * TW_PER_PAGE;
	w.to = w.from + TW_PER_PAGE;
	w.fits = tw_head_put(buf, len, &w.at, proto_v, page);
	if (!w.fits) {
		return 0;
	}
	walk(&w);
	if (!w.fits || !tw_tail_put(buf, len, &w.at, w.n, w.n > w.to, w.gen)) {
		return 0;
	}
	return (int)w.at;
}

enum tw_aim tw_aim(uint32_t gen, int index, int *x, int *y, bool force)
{
	struct walk w = { 0 };

	w.want = index;
	walk(&w);
	if (w.gen != gen) {
		return TW_AIM_STALE;
	}
	/* The listing is the one up now and has no such entry: the PC's
	 * mistake, not the panel's change, and said so (review, N1). */
	if (!w.found) {
		return TW_AIM_RANGE;
	}
	if (w.disabled && !force) {
		return TW_AIM_DISABLED;
	}
	if (w.aim == TW_AIM_OK) {
		*x = w.ax;
		*y = w.ay;
	}
	return w.aim;
}

static const char *aim_word(enum tw_aim a)
{
	switch (a) {
	case TW_AIM_STALE:
		return "stale";
	case TW_AIM_RANGE:
		return "range";
	case TW_AIM_DISABLED:
		return "disabled";
	case TW_AIM_COVERED:
		return "covered";
	default:
		return NULL;
	}
}

/* Every field here is an index, a hold or a flag: -1 for absent, NaN, or out
 * of range, which then refuses as args. */
static int num_field(const char *json, const char *key, int max)
{
	double v;

	if (!msg_get_double(json, key, &v) || v != v || v < 0 || v > max) {
		return -1;
	}
	return (int)v;
}

const char *tw_click_parse(const char *json, bool busy, struct ti_stroke *s)
{
	double g;
	int i = num_field(json, "i", 9999);
	bool force = num_field(json, "force", 1) == 1;
	enum tw_aim a;

	memset(s, 0, sizeof(*s));
	if (i < 0 || !msg_get_double(json, "gen", &g) || g != g || g < 0 ||
	    g > 4294967295.0) {
		return "args";
	}
	/* A slide, a queued open or close, or a screen load still running:
	 * LVGL drops input during a screen-load animation (disp->prev_scr),
	 * so a press now would say done and press nothing (review, N3). */
	if (busy || lv_display_get_screen_prev(NULL) != NULL) {
		return "busy";
	}
	a = tw_aim((uint32_t)g, i, &s->x0, &s->y0, force);
	if (a != TW_AIM_OK) {
		return aim_word(a);
	}
	s->op = TI_OP_TAP;
	s->ms = 120;
	if (msg_get_double(json, "ms", &g)) {
		s->ms = num_field(json, "ms", TI_MAX_MS);
	}
	return ti_valid(s) ? NULL : "args";
}

int tw_widgets_reply(const char *json, char *buf, size_t len, int proto_v)
{
	int p = num_field(json, "page", 999);

	return tw_page_line(buf, len, proto_v, p < 0 ? 0 : p);
}
