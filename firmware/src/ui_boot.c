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
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/display.h>
#include <lvgl.h>

#include "ui_boot.h"
#include "proto.h"
#include "bootanim.h"
#include "bootanim_dec.h"

static const struct device *const boot_disp =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_display));

static lv_obj_t *scr;
static uint8_t strip_buf[4096];

/* Survives a warm reset; the magic guards against power-on garbage. */
static __noinit uint32_t skip_magic;
#define SKIP_MAGIC 0xb007512bu

void ui_boot_mark_intentional_reboot(void)
{
	skip_magic = SKIP_MAGIC;
}

/* Pump UI + protocol for `ms`, so the splash doubles as the daemon-detect
 * window. */
static void pump(int ms)
{
	int64_t end = k_uptime_get() + ms;

	while (k_uptime_get() < end) {
		proto_service();
		lv_timer_handler();
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
static bool bootanim_play(const uint8_t *blob, size_t len)
{
	struct ba_header hdr;
	size_t off;

	if (!ba_parse_header(blob, len, &hdr, &off))
		return false;

	int64_t next = k_uptime_get();

	for (int i = 0; i < hdr.nframes; i++) {
		if (ba_decode_frame(blob, len, &off, strip_buf,
				    sizeof(strip_buf), blit_cb, NULL) < 0)
			return false;
		next += 1000 / hdr.fps;
		while (k_uptime_get() < next) {
			proto_service();
			lv_timer_handler();
			k_sleep(K_MSEC(5));
		}
	}
	return true;
}

void ui_boot_splash(void)
{
	bool skip = (skip_magic == SKIP_MAGIC);

	skip_magic = 0;

	scr = lv_obj_create(NULL);
	lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_set_style_bg_color(scr, lv_color_hex(BOOTANIM_BG_RGB), 0);
	lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
	lv_scr_load(scr);
	lv_refr_now(NULL);	/* the one and only LVGL paint of this screen */

	if (skip) {
		/* Same picture, no theater: the final frame lands statically
		 * and the dwell shrinks to the daemon round-trip (hello went
		 * out in proto_init; a live daemon answers within
		 * milliseconds). The screen stays up through the WiFi scan
		 * either way. */
		(void)bootanim_play(bootanim_last, sizeof(bootanim_last));
		pump(300);
		return;
	}

	if (!bootanim_play(bootanim_blob, sizeof(bootanim_blob))) {
		/* Corrupt blob: keep the bare screen up for the same
		 * daemon-detect window the old splash guaranteed. */
		pump(2500);
	}
}

void ui_boot_teardown(void)
{
	if (scr) {
		lv_obj_del(scr);
		scr = NULL;
	}
}
