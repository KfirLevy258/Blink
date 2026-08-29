/*
 * Boot splash: the encoded eyes clip (see tools/encode_bootanim.py), which
 * doubles as the mode-detection window: proto_service() runs between frames,
 * so a PC daemon's reply to our boot-time hello is already in by the time
 * main() decides USB vs WiFi. No selection screen -- v1 had one, and on
 * hardware it was pure friction (the answer is always detectable: a daemon
 * talks, or it doesn't).
 *
 * The panel is the framebuffer: frames are delta-RLE rects streamed with
 * display_write() through a small strip buffer, and the ILI9341's GRAM
 * retains everything between writes (there is no RAM for a 150 KB frame).
 * LVGL coexists by ordering, not locking: the loaded screen is a bare
 * rectangle in the clip's background color, flushed exactly once; nothing
 * invalidates it afterwards, so the pump's lv_timer_handler() calls never
 * repaint over the streamed frames.
 *
 * The device reboots itself on purpose all over the setup flow (the driver
 * only joins from a clean boot). Replaying the full animation after each of
 * those made every reboot feel like a fresh power-on, so intentional reboots
 * mark themselves in noinit RAM and the next boot renders only the clip's
 * final frame, with just enough of a window for the daemon handshake.
 *
 * A company unit (see logo_parse.h) follows the clip with its logo: the
 * screen cuts to the logo's background, the logo clip streams the same way,
 * and the last frame holds for the header's hold time. Same player, same
 * strip buffer, same protocol pump between frames -- a company boot is a
 * longer boot, not a different one. Intentional reboots skip it with the
 * clip, for the same reason.
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/display.h>
#include <zephyr/sys/printk.h>
#include <lvgl.h>

#include "ui_boot.h"
#include "proto.h"
#include "bootclip.h"
#include "bootanim_dec.h"
#include "logo.h"

static const struct device *const boot_disp =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_display));

static lv_obj_t *scr;
/* Borrowed from the LVGL pool for the splash only. A permanent 4 KB static
 * here starved the WiFi driver's runtime heap: large TX frames silently
 * dropped while small ones passed, breaking the captive portal in
 * size-correlated ways (diagnosed on hardware 2026-07-16). */
#define STRIP_BYTES 4096
static uint8_t *strip_buf;

/* Survives a warm reset; the magic guards against power-on garbage. */
static __noinit uint32_t skip_magic;
#define SKIP_MAGIC 0xb007512bu

void ui_boot_mark_intentional_reboot(void)
{
	skip_magic = SKIP_MAGIC;
}

bool ui_boot_intentional_pending(void)
{
	return skip_magic == SKIP_MAGIC;
}

/*
 * Set by main.c to its watchdog feeder. NULL until then, and NULL for good on
 * an ordinary boot -- the feeder is a no-op unless this is an unconfirmed test
 * boot, but calling through a hook keeps that knowledge in main.c.
 */
static void (*host_pump)(void);

void ui_boot_set_pump(void (*fn)(void))
{
	host_pump = fn;
}

static void host_pump_run(void)
{
	if (host_pump) {
		host_pump();
	}
}

/* Pump UI + protocol for `ms`, so the splash doubles as the daemon-detect
 * window. */
static void pump(int ms)
{
	int64_t end = k_uptime_get() + ms;

	while (k_uptime_get() < end) {
		proto_service();
		lv_timer_handler();
		host_pump_run();
		k_sleep(K_MSEC(10));
	}
}

static void blit_cb(uint16_t x, uint16_t y, uint16_t w, uint16_t h,
		    const uint8_t *pix, void *user)
{
	struct display_buffer_descriptor desc = {
		.buf_size = (size_t)w * h * 2,
		.width = w,
		.height = h,
		.pitch = w,
	};

	ARG_UNUSED(user);
	display_write(boot_disp, x, y, &desc, pix);
}

/* Play a BAN1 blob; frame pacing comes from the blob header, and the gaps
 * between frames keep servicing the daemon protocol. Returns false if the
 * blob is corrupt (bad header or a frame fails to decode), true once every
 * frame has played. */
static bool bootanim_play_ex(const uint8_t *blob, size_t len,
			     bool (*stop)(void))
{
	struct ba_header hdr;
	size_t off;

	if (!ba_parse_header(blob, len, &hdr, &off))
		return false;

	int64_t next = k_uptime_get();

	for (int i = 0; i < hdr.nframes; i++) {
		if (ba_decode_frame(blob, len, &off, strip_buf,
				    STRIP_BYTES, blit_cb, NULL) < 0)
			return false;
		next += 1000 / hdr.fps;
		while (k_uptime_get() < next) {
			proto_service();
			lv_timer_handler();
			host_pump_run();
			k_sleep(K_MSEC(5));
		}
		/* Between frames, never mid-frame: a clip cut here leaves a
		 * whole picture on the panel. The sleep loop is cut this way
		 * the moment the host speaks again. */
		if (stop != NULL && stop()) {
			return true;
		}
	}
	return true;
}

static bool bootanim_play(const uint8_t *blob, size_t len)
{
	return bootanim_play_ex(blob, len, NULL);
}

bool ui_boot_play_clip(const uint8_t *blob, size_t len, bool (*stop)(void))
{
	bool own = (strip_buf == NULL);

	if (own) {
		strip_buf = lv_malloc(STRIP_BYTES);
		if (strip_buf == NULL) {
			return false;
		}
	}
	bool ok = bootanim_play_ex(blob, len, stop);

	if (own) {
		lv_free(strip_buf);
		strip_buf = NULL;
	}
	return ok;
}

/* The company logo, on units that have one. Runs with strip_buf held. */
static void logo_show(void)
{
	const struct logo_info *lg = logo_active();

	if (lg == NULL) {
		return;
	}

	/* The second and last LVGL paint of this screen: the logo's stage.
	 * The style change invalidates the screen, so this repaint is a full
	 * one, and nothing invalidates it again until teardown. */
	lv_obj_set_style_bg_color(scr, lv_color_hex(lg->bg_rgb), 0);
	lv_refr_now(NULL);

	if (!bootanim_play(lg->blob, lg->blob_len)) {
		/* The CRC passed, so this is a bug in the tool or the decoder
		 * rather than a bad flash; say so and move on. */
		printk("[boot] logo: frame failed to decode\n");
		return;
	}
	pump(lg->hold_ms);
}

void ui_boot_splash(void)
{
	const struct bootclip *clip = bootclip_active();
	bool skip = (skip_magic == SKIP_MAGIC);

	skip_magic = 0;

	scr = lv_obj_create(NULL);
	lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
	/* Intentional reboots wear the UI background, not the clip's clay:
	 * even 300 ms of brand orange between two dark screens betrayed the
	 * restart (user feedback 2026-07-16). Cold boots keep the clay so
	 * the clip has its stage. */
	lv_obj_set_style_bg_color(scr, skip ? lv_color_hex(0x0E1116)
					    : lv_color_hex(clip->bg_rgb), 0);
	lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
	lv_scr_load(scr);
	lv_refr_now(NULL);	/* the one and only LVGL paint of this screen */

	if (skip) {
		/* No face, no theater -- just a dark frame while the daemon
		 * round-trip window runs (hello went out in proto_init; a
		 * live daemon answers within milliseconds). The screen stays
		 * up through the WiFi scan either way. Flashing the clip's
		 * final frame here read as an unexplained face pop during
		 * setup reboots (user feedback 2026-07-16); bootanim_last
		 * stays available in the generated header, and the linker
		 * drops it while unreferenced. */
		pump(300);
		return;
	}

	strip_buf = lv_malloc(STRIP_BYTES);
	if (strip_buf) {
		bool ok = bootanim_play(clip->blob, clip->blob_len);

		if (ok) {
			logo_show();
		}
		lv_free(strip_buf);
		strip_buf = NULL;
		if (ok) {
			return;
		}
	}
	/* No RAM for the player, or a corrupt blob: keep the bare screen up
	 * for the same daemon-detect window the old splash guaranteed. */
	pump(2500);
}

void ui_boot_teardown(void)
{
	if (scr) {
		lv_obj_del(scr);
		scr = NULL;
	}
}
