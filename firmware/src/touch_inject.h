#ifndef TOUCH_INJECT_H
#define TOUCH_INJECT_H

/*
 * A finger the PC can drive, for the bench only (CONFIG_BLINK_TOUCH_INJECT).
 *
 * The owner wants every on-board interaction tested without a human at the
 * panel: open Settings, pick a theme, turn Focus on, swipe the eyes in, read
 * the [mem] line each one leaves. tools/board_touch.py sends strokes as
 * protocol lines and this plays them into the touch controller's own input
 * device, so LVGL's pointer glue and ui_swipe's callback both see exactly
 * what a panel report looks like. Nothing downstream knows the difference,
 * which is the whole point: a test that went round the real input path would
 * prove the screens, not the touch handling that has broken most often.
 *
 * Never in a shipped image. A board that accepts touches from the cable is a
 * board anything on the computer can operate -- including "Edit on computer"
 * and the update row -- so the option defaults off, release.sh and
 * flash_encrypted.sh refuse an image carrying it, and burn.sh flashes one
 * only with --bench (tools/lib_selftest.sh).
 *
 * Wire, all v2 -- every line also carries "v":2, left out below:
 *
 *   PC    {"t":"touch","op":"tap","x":X,"y":Y,"ms":HOLD}
 *         {"t":"touch","op":"swipe","x0":..,"y0":..,"x1":..,"y1":..,"ms":T}
 *         {"t":"touch","op":"press","x":X,"y":Y}
 *         {"t":"touch","op":"release"}
 *         {"t":"touch","op":"dismiss"}   take down the notice popup
 *         {"t":"touch","op":"wake"}      end a doze (ui_sleep.c)
 *   board {"t":"touch_done","op":OP,"ok":1}        once the stroke is played
 *         ...,"auto":1 on a release nobody asked for: a press that let
 *         go by itself at TI_HOLD_MAX_MS
 *         {"t":"touch_refused","why":"busy"|"args"|"not_held"}
 *
 *   PC    {"t":"ui_state"}
 *   board {"t":"ui_state","scr":..,"ov":..,"modal":..,"busy":0|1,
 *          "theme":N,"stored":N,"tp":N,"focus":..,"bl":N,"edit":..,
 *          "slp":0|1,"nt":none|sleep|ota|other,"gf":off|session|weekly}
 *
 * Screen coordinates throughout, 320 x 240, origin top left.
 */
#include <stdbool.h>

#ifdef CONFIG_BLINK_TOUCH_INJECT
/* Say on the console that this is a bench image. Once, at proto_init. */
void touch_inject_init(void);

/* A protocol line of type `type`. True when it was one of ours. Main thread,
 * from proto.c's dispatch. */
bool touch_inject_dispatch(const char *type, const char *json);

/* Send what the player has finished since the last call. Main thread, from
 * proto_service(), so the reply is never printed from the workqueue in the
 * middle of a line the main thread is writing. */
void touch_inject_service(void);
#endif

#endif /* TOUCH_INJECT_H */
