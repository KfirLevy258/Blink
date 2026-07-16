/*
 * BAN1 boot-animation blob decoder. Pure C, no Zephyr/LVGL dependencies,
 * so tests/bootanim/host_test.c can exercise it on the dev machine.
 * Format documentation lives with the encoder: tools/encode_bootanim.py.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct ba_header {
	uint16_t w, h;
	uint8_t fps;
	uint8_t flags;
	uint16_t nframes;
};

/* Pixels arrive as opaque 2-byte pairs already in panel byte order. */
typedef void (*ba_blit_fn)(uint16_t x, uint16_t y, uint16_t w, uint16_t h,
			   const uint8_t *pixels, void *user);

bool ba_parse_header(const uint8_t *blob, size_t len,
		     struct ba_header *hdr, size_t *off);

/* Decode the frame at *off, blitting each rect in strips of whole rows
 * sized to fit strip_bytes; advances *off past the frame. Returns the
 * frame's rect count (0 = hold previous frame), or -1 on malformed input. */
int ba_decode_frame(const uint8_t *blob, size_t len, size_t *off,
		    uint8_t *strip, size_t strip_bytes,
		    ba_blit_fn blit, void *user);
