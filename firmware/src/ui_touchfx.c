/*
 * Touch echo: a light, half-transparent circle that blooms at the press point
 * and fades out. Pure visual feedback -- it lives on lv_layer_top() so it shows
 * over any screen, and is non-clickable so it never eats the touch it echoes.
 *
 * A 20 ms timer watches the pointer indev for a press-DOWN edge; on each one it
 * parks the circle at that point and (re)starts two animations -- a small
 * expand and a fade to transparent -- then hides it when the fade completes.
 */
#include <zephyr/kernel.h>
#include <lvgl.h>

#include "ui_touchfx.h"

#define FX_COLOR	0xFFFFFF	/* soft white reads on both dark and light */
#define FX_START	18		/* diameter (px) at the moment of the press */
#define FX_END		44		/* diameter (px) once fully faded */
#define FX_OPA		120		/* starting opacity (~47%, i.e. "half") */
#define FX_MS		420		/* bloom + fade duration */

static lv_obj_t *echo;
static int32_t cx, cy;		/* press centre, so the expand stays centred */

static void size_cb(void *var, int32_t d)
{
	ARG_UNUSED(var);
	lv_obj_set_size(echo, d, d);
	lv_obj_set_pos(echo, cx - d / 2, cy - d / 2);
}

static void opa_cb(void *var, int32_t o)
{
	ARG_UNUSED(var);
	lv_obj_set_style_bg_opa(echo, (lv_opa_t)o, 0);
}

static void done_cb(lv_anim_t *a)
{
	ARG_UNUSED(a);
	lv_obj_add_flag(echo, LV_OBJ_FLAG_HIDDEN);
}

static void poll_cb(lv_timer_t *t)
{
	ARG_UNUSED(t);
	static bool was_pressed;
	lv_indev_t *in = NULL;

	while ((in = lv_indev_get_next(in)) != NULL) {
		if (lv_indev_get_type(in) == LV_INDEV_TYPE_POINTER) {
			break;
		}
	}
	if (!in) {
		return;
	}

	bool pressed = lv_indev_get_state(in) == LV_INDEV_STATE_PRESSED;

	if (pressed && !was_pressed) {		/* press-down edge: bloom here */
		lv_point_t p;
		lv_anim_t a;

		lv_indev_get_point(in, &p);
		cx = p.x;
		cy = p.y;
		lv_obj_move_foreground(echo);
		lv_obj_clear_flag(echo, LV_OBJ_FLAG_HIDDEN);

		/* Starting an anim with the same var+exec_cb replaces any still
		 * running, so a rapid double-tap simply restarts the bloom. */
		lv_anim_init(&a);
		lv_anim_set_var(&a, echo);
		lv_anim_set_duration(&a, FX_MS);
		lv_anim_set_path_cb(&a, lv_anim_path_ease_out);

		lv_anim_set_exec_cb(&a, size_cb);
		lv_anim_set_values(&a, FX_START, FX_END);
		lv_anim_start(&a);

		lv_anim_set_exec_cb(&a, opa_cb);
		lv_anim_set_values(&a, FX_OPA, 0);
		lv_anim_set_completed_cb(&a, done_cb);
		lv_anim_start(&a);
	}
	was_pressed = pressed;
}

void ui_touchfx_init(void)
{
	echo = lv_obj_create(lv_layer_top());
	lv_obj_set_size(echo, FX_START, FX_START);
	lv_obj_set_style_radius(echo, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_bg_color(echo, lv_color_hex(FX_COLOR), 0);
	lv_obj_set_style_bg_opa(echo, FX_OPA, 0);
	lv_obj_set_style_border_width(echo, 0, 0);
	lv_obj_set_style_shadow_width(echo, 0, 0);
	lv_obj_clear_flag(echo, LV_OBJ_FLAG_CLICKABLE);
	lv_obj_clear_flag(echo, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_add_flag(echo, LV_OBJ_FLAG_HIDDEN);
	lv_timer_create(poll_cb, 20, NULL);
}
