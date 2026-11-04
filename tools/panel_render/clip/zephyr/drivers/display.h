/* Host stub: the panel the eye clip streams its frames to. There is none
 * here, and the harness gives the clip no frames to stream (stubs.c), so
 * display_write() only has to link. The devicetree macros resolve to a
 * NULL device for the same reason. */
#ifndef HOST_CLIP_DISPLAY_H
#define HOST_CLIP_DISPLAY_H
#include <stddef.h>
#include <stdint.h>

struct device;

struct display_buffer_descriptor {
	uint32_t buf_size;
	uint16_t width;
	uint16_t height;
	uint16_t pitch;
};

#define DT_CHOSEN(node)		0
#define DT_PARENT(node)		0
#define DEVICE_DT_GET(node)	((const struct device *)NULL)

int display_write(const struct device *dev, uint16_t x, uint16_t y,
		  const struct display_buffer_descriptor *desc,
		  const void *buf);
#endif
