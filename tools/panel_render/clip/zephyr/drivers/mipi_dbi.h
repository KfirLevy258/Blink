/* Host stub: the panel's command bus, which ui_slide.c uses for the scroll
 * registers. A wipe (UI_SLIDE_WIPE) never scrolls, and nothing here has a
 * scroll register, so the writes succeed and do nothing. */
#ifndef HOST_CLIP_MIPI_DBI_H
#define HOST_CLIP_MIPI_DBI_H
#include <stddef.h>
#include <stdint.h>
#include <zephyr/drivers/display.h>

#define SPI_OP_MODE_MASTER	0
#define SPI_WORD_SET(n)		0

struct mipi_dbi_config {
	int unused;
};

#define MIPI_DBI_CONFIG_DT(node, op, delay)	{ 0 }

int mipi_dbi_command_write(const struct device *dev,
			   const struct mipi_dbi_config *cfg, uint8_t cmd,
			   const uint8_t *data, size_t len);
#endif
