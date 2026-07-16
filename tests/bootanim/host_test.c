/* Standalone host test for bootanim_dec (macOS-friendly; native_sim is
 * Linux-only).
 * Build & run:
 *   cc -I ../../firmware/src host_test.c ../../firmware/src/bootanim_dec.c \
 *      -o /tmp/batest && /tmp/batest
 */
#include <stdio.h>
#include <string.h>
#include "bootanim_dec.h"

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

/* Hand-built BAN1 blob per the format documented in
 * tools/encode_bootanim.py: 4x3 canvas, 12 fps, 2 frames.
 * Frame 0: one full-canvas rect, a single RLE run of 12 x 0xBEEF
 *          (ctrl 0x8A = run of 12).
 * Frame 1: one 2x1 rect at (1,1), two literal pixels 0x1234 0x5678
 *          (ctrl 0x01 = 2 literals). */
static const uint8_t blob[] = {
	'B', 'A', 'N', '1', 4, 0, 3, 0, 12, 1, 2, 0,
	/* frame 0 */ 1,
	0, 0, 0, 0, 4, 0, 3, 0, 3, 0, 0, 0,
	0x8a, 0xbe, 0xef,
	/* frame 1 */ 1,
	1, 0, 1, 0, 2, 0, 1, 0, 5, 0, 0, 0,
	0x01, 0x12, 0x34, 0x56, 0x78,
};

static uint8_t canvas[3][4][2];
static int blits;

static void blit(uint16_t x, uint16_t y, uint16_t w, uint16_t h,
		 const uint8_t *pix, void *user)
{
	(void)user;
	blits++;
	for (uint16_t r = 0; r < h; r++)
		memcpy(&canvas[y + r][x][0], pix + (size_t)r * w * 2,
		       (size_t)w * 2);
}

int main(void)
{
	struct ba_header hdr;
	size_t off;

	CHECK(ba_parse_header(blob, sizeof(blob), &hdr, &off), "header parses");
	CHECK(hdr.w == 4 && hdr.h == 3, "dimensions 4x3");
	CHECK(hdr.fps == 12 && hdr.nframes == 2, "fps and frame count");
	CHECK(off == 12, "header is 12 bytes");
	CHECK(!ba_parse_header((const uint8_t *)"nope", 4, &hdr, &off),
	      "bad magic rejected");

	/* A strip of 4 pixels forces frame 0 (12 px) through three blits,
	 * so the single RLE run must survive strip boundaries. */
	uint8_t strip[8];

	CHECK(ba_decode_frame(blob, sizeof(blob), &off, strip, sizeof(strip),
			      blit, NULL) == 1, "frame 0 decodes, 1 rect");
	CHECK(blits == 3, "frame 0 split into 3 strips");
	CHECK(canvas[0][0][0] == 0xbe && canvas[0][0][1] == 0xef &&
	      canvas[2][3][0] == 0xbe && canvas[2][3][1] == 0xef,
	      "run fills first and last pixel");

	CHECK(ba_decode_frame(blob, sizeof(blob), &off, strip, sizeof(strip),
			      blit, NULL) == 1, "frame 1 decodes, 1 rect");
	CHECK(canvas[1][1][0] == 0x12 && canvas[1][1][1] == 0x34 &&
	      canvas[1][2][0] == 0x56 && canvas[1][2][1] == 0x78,
	      "literal pixels land at (1,1)-(2,1)");
	CHECK(canvas[1][0][0] == 0xbe, "untouched pixel keeps frame 0 value");
	CHECK(off == sizeof(blob), "offset lands at end of blob");

	/* Truncation must fail cleanly, never overread. */
	off = 12;
	CHECK(ba_decode_frame(blob, 20, &off, strip, sizeof(strip),
			      blit, NULL) == -1, "truncated blob -> -1");

	printf("%s\n", failures ? "FAILURES" : "all ok");
	return failures != 0;
}
