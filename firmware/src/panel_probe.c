/*
 * TEMPORARY (CYD-T2/T3): prove the panel and identify it by eye.
 *
 * The controller's ID registers cannot be read on this board -- the CYD does not
 * bring the panel's MISO back to the SoC, so every read returns zeros. The two-
 * USB-port batch ships either an ILI9341 or an ST7789, so the panel is instead
 * identified empirically: drive it as an ILI9341 and look at it.
 *
 * Draws four full-height vertical bars -- RED, GREEN, BLUE, WHITE, left to
 * right -- plus a 1px white border around the very edge of the panel. One glance
 * then answers three questions at once:
 *
 *   red, green, blue, white   -> ILI9341, correct
 *   cyan, magenta, yellow, black -> colours inverted (add display-inversion)
 *   blue, green, red, white   -> RGB/BGR channel order swapped
 *   nothing / garbage         -> not an ILI9341; switch to st7789v
 *
 * The border catches a wrong width/height or rotation: all four edges must show.
 *
 * Deleted once the panel is confirmed and LVGL takes over.
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/display.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/byteorder.h>
#include <string.h>

#include "panel_probe.h"

static const struct device *const display_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));
static const struct gpio_dt_spec backlight =
	GPIO_DT_SPEC_GET(DT_NODELABEL(backlight), gpios);

/* RGB565, big-endian on the wire. */
#define RGB565(r, g, b) ((uint16_t)(((r) & 0xF8) << 8 | ((g) & 0xFC) << 3 | ((b) >> 3)))

#define C_RED   RGB565(0xFF, 0x00, 0x00)
#define C_GREEN RGB565(0x00, 0xFF, 0x00)
#define C_BLUE  RGB565(0x00, 0x00, 0xFF)
#define C_WHITE RGB565(0xFF, 0xFF, 0xFF)
#define C_BLACK 0x0000

/* One row of pixels at a time keeps RAM use trivial (no framebuffer). */
static uint16_t row[320];

static void fill_row(uint16_t *buf, uint16_t w, uint16_t colour)
{
	for (uint16_t i = 0; i < w; i++) {
		buf[i] = sys_cpu_to_be16(colour);
	}
}

void panel_probe(void)
{
	struct display_capabilities cap;

	if (!device_is_ready(display_dev)) {
		printk("[probe] display device NOT ready -- driver failed to init\n");
		return;
	}
	if (gpio_is_ready_dt(&backlight)) {
		gpio_pin_configure_dt(&backlight, GPIO_OUTPUT_ACTIVE);
	}

	display_get_capabilities(display_dev, &cap);
	printk("[probe] display ready: %ux%u, pixel format %u\n",
	       cap.x_resolution, cap.y_resolution, cap.current_pixel_format);

	const uint16_t w = cap.x_resolution;
	const uint16_t h = cap.y_resolution;
	const uint16_t bar = w / 4;

	struct display_buffer_descriptor desc = {
		.buf_size = w * 2,
		.width = w,
		.height = 1,
		.pitch = w,
	};

	for (uint16_t y = 0; y < h; y++) {
		if (y == 0 || y == h - 1) {
			fill_row(row, w, C_WHITE); /* top / bottom border */
		} else {
			for (uint16_t x = 0; x < w; x++) {
				uint16_t c;

				if (x == 0 || x == w - 1) {
					c = C_WHITE; /* left / right border */
				} else if (x < bar) {
					c = C_RED;
				} else if (x < bar * 2) {
					c = C_GREEN;
				} else if (x < bar * 3) {
					c = C_BLUE;
				} else {
					c = C_WHITE;
				}
				row[x] = sys_cpu_to_be16(c);
			}
		}
		display_write(display_dev, 0, y, &desc, row);
	}

	display_blanking_off(display_dev);
	printk("[probe] colour bars drawn: expect RED GREEN BLUE WHITE, left to right\n");
	printk("[probe] plus a 1px white border on all four edges\n");
}
