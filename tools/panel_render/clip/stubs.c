/*
 * What ui_anim.c, ui_slide.c and ui_sleep.c reach for that is not the gauge
 * screen: the panel's two drivers, the clips, the touch echo, the settings
 * notices, and the clip player. None of it decides anything the harness
 * checks, and each stands in the way the board's version would look from
 * the gauge screen.
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/display.h>
#include <zephyr/drivers/mipi_dbi.h>
#include <lvgl.h>

#include "bootclip.h"
#include "proto.h"
#include "ui_boot.h"
#include "ui_settings.h"
#include "ui_touchfx.h"

/* The eye clip's frames, and the panel's scroll registers: nowhere to go. */
int display_write(const struct device *dev, uint16_t x, uint16_t y,
		  const struct display_buffer_descriptor *desc, const void *buf)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(x);
	ARG_UNUSED(y);
	ARG_UNUSED(desc);
	ARG_UNUSED(buf);
	return 0;
}

int mipi_dbi_command_write(const struct device *dev,
			   const struct mipi_dbi_config *cfg, uint8_t cmd,
			   const uint8_t *data, size_t len)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(cfg);
	ARG_UNUSED(cmd);
	ARG_UNUSED(data);
	ARG_UNUSED(len);
	return 0;
}

/*
 * No blob, so ui_anim.c's player takes its "no RAM or corrupt blob" path:
 * it holds the flat clay overlay and keeps pumping until the swipe out. That
 * is the whole of what the gauge screen can see of a clip -- its children
 * hidden for the length of the show, the protocol and LVGL running -- and
 * nothing here has a panel to stream frames to anyway.
 */
static const struct bootclip clay = {
	.blob = NULL, .blob_len = 0, .bg_rgb = 0xC08060, .name = "host",
};

const struct bootclip *bootclip_active(void)
{
	return &clay;
}

const struct bootclip *sleepclip_active(enum sleep_part part)
{
	ARG_UNUSED(part);
	return &clay;
}

void ui_touchfx_suspend(bool on)
{
	ARG_UNUSED(on);
}

/* The peek's note and What's new: cards on lv_layer_top, not the gauge
 * screen's children. */
void ui_settings_notice(const char *txt)
{
	ARG_UNUSED(txt);
}

void ui_settings_notice_dismiss(void)
{
}

void ui_settings_whatsnew_dismiss(void)
{
}

bool ui_settings_busy(void)
{
	return false;
}

/*
 * ui_boot.c's bootanim_play_ex() with nothing to decode: the same frame
 * pacing, and between frames the same three calls -- the protocol, LVGL's
 * timers, a 5 ms sleep -- because those are what the gauge screen lives on
 * for the length of a doze. Two seconds a loop, 15 frames a second, and the
 * stop test between frames as the real one has it.
 */
bool ui_boot_play_clip(const uint8_t *blob, size_t len, bool (*stop)(void))
{
	int64_t next = k_uptime_get();

	ARG_UNUSED(blob);
	ARG_UNUSED(len);
	for (int i = 0; i < 30; i++) {
		next += 1000 / 15;
		while (k_uptime_get() < next) {
			proto_service();
			lv_timer_handler();
			k_sleep(K_MSEC(5));
		}
		if (stop != NULL && stop()) {
			return true;
		}
	}
	return true;
}
