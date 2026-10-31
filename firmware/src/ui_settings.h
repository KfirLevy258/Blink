#ifndef UI_SETTINGS_H
#define UI_SETTINGS_H

#include <lvgl.h>

/* Make `scr` (the gauge screen) open the settings panel on a left swipe.
 * The panel is an overlay on the same screen, NOT a separate LVGL screen:
 * the LVGL pool cannot hold two full screens (see usage_view_deinit). It was
 * 16 KiB when that was written and is 32 KiB now (prj.conf,
 * LV_Z_MEM_POOL_SIZE), and two screens still do not fit: the gauges and an
 * open Settings come to about 25 KB on the board before any popup.
 * It holds brightness, theme, Focus and the software update; the
 * reset-and-reboot actions went with the standalone WiFi mode on
 * 2026-10-27. */
void ui_settings_attach(lv_obj_t *scr);

/* One-line notice popup on the gauge screen (update outcome etc.). Safe to
 * call once the screen exists; replaces any previous notice. */
void ui_settings_notice(const char *txt);
/* The same box with a heading above the body: the heading in montserrat_16
 * at full brightness, the body dim beneath the flex gap. For a notice that
 * is a headline plus detail rather than one sentence -- see the comment on
 * the implementation. A NULL or empty title is exactly ui_settings_notice. */
void ui_settings_notice_titled(const char *title, const char *body);
/* The post-update notice: a tick, the version, "5 changes since 1.2.5", and
 * a "What's new" button that opens the paged list. `from`/`to` are the halves
 * of the OTA breadcrumb, and they decide what that screen shows. */
void ui_settings_notice_update(const char *version, const char *summary,
			       const char *from, const char *to);
/* Close the What's new screen, if it is open. */
void ui_settings_whatsnew_dismiss(void);
/* Take a notice down without the tap; the sleep peek does this on timeout. */
void ui_settings_notice_dismiss(void);

/* Run any pending open/close transition. MUST be called from a mode loop in
 * thread context, not from an LVGL callback: the transition drives
 * lv_refr_now() itself (see ui_slide.h). `pump` (may be NULL) runs each step
 * so background duties and the boot watchdog stay alive. */
/*
 * Discard any open/close the handlers latched but nobody serviced.
 *
 * ui_settings_attach() arms the gestures before the mode loop exists. In the
 * standalone WiFi mode (removed 2026-10-27) main.c ran a network scan and a
 * WiFi settle in between, which pumped lv_timer_handler() with touch fully
 * live and could take a minute on a failed join; a tap latched in that window
 * was acted on by the mode loop's first iteration -- the panel sliding in by
 * itself long after the user gave up on it. The gap is short now, but the
 * call costs nothing. Call this once before entering the mode loop; a
 * deliberate tap repeats, and from there on the latch is serviced within one
 * iteration.
 */
void ui_settings_drop_pending(void);

void ui_settings_service(void (*pump)(void));

/*
 * Is the panel open, or is a gesture still waiting to be serviced?
 *
 * Both answers mean the same thing to a caller that is about to stop running
 * the mode loop: a person is in the middle of using this panel, and the only
 * code that can act on them is the loop that is about to go away. The doze in
 * ui_sleep.c asks it as a reason to wake, and main.c as a reason not to fall
 * asleep in the first place -- because settings is where an owner goes when
 * the board is behaving badly, which is precisely when it is dozing.
 */
bool ui_settings_busy(void);

/* Repaint the settings panel, if it is open, in the active theme. */
void ui_settings_apply_theme(void);

/* The daemon answered "Edit on computer" (an explicit theme_edit ok): ok
 * means a browser opened. From a daemon that promises done:1, ok 1 holds the
 * slot until the builder ends, whenever it arrives (edit_hold.h); otherwise
 * a no-op unless the row is still waiting for an answer. */
void ui_settings_theme_edit_reply(bool ok);

/* A theme preview or save line arrived: a builder is at work. Answers an ask
 * still in flight, and while held starts the hold's upper bound again. */
void ui_settings_theme_edit_builder_seen(void);

/* The builder the daemon opened for "Edit on computer" has ended (theme
 * contract Addendum 6): the hold goes, whatever note the slot shows goes,
 * and it reads its name again. An ask still in flight keeps its answer. */
void ui_settings_theme_edit_done(void);

/* A welcome line, whole: its caps say whether this daemon sends done:1, and
 * its "editing" field whether a builder it opened is still open. */
void ui_settings_theme_edit_welcome(const char *json);

/* An edit_state line: the daemon restating whether that builder is open
 * (pc/protocol.edit_state). Holds, frees, or starts the bound again. */
void ui_settings_theme_edit_state(bool editing);

/* The host went away (HOST LOST) or said bye: the hold goes. */
void ui_settings_theme_edit_host_gone(void);

#ifdef CONFIG_BLINK_TOUCH_INJECT
/*
 * What Settings is showing, for the bench injector's ui_state reply
 * (touch_inject.h). Read from the same pointers the screens are built and
 * torn down through, so it cannot disagree with what is on the panel. Main
 * thread only. Never in a release image: the option is off there.
 */
struct ui_settings_bench {
	bool open;		/* the panel is up (or is the ground under one) */
	const char *sub;	/* none|brightness|theme|focus|confirm */
	const char *modal;	/* none|notice|whatsnew|prompt|download */
	bool busy;		/* a transition is latched or a close queued */
	int theme_page;		/* the theme screen's page, from 0 */
	const char *edit;	/* "Edit on computer": name|waiting|opened|run|held */
	const char *nt;		/* the notice up: none|sleep|ota|other */
};
void ui_settings_bench_state(struct ui_settings_bench *out);

/* Say what the notice just put up is about ("sleep", "ota"). Cleared
 * wherever a notice is created or deleted, so a later notice from anywhere
 * else reads as "other" rather than inheriting the word. */
void ui_settings_bench_notice_is(const char *kind);
#endif

#endif /* UI_SETTINGS_H */
