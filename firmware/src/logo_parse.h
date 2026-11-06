#ifndef LOGO_PARSE_H
#define LOGO_PARSE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * BLGO: the company boot logo, as written to the `logo` flash partition by
 * the factory (tools/encode_logo.py, flashed by tools/burn.sh --logo).
 *
 * There is deliberately no "is this a company unit" flag anywhere else. The
 * partition IS the flag: a unit built for an individual has never had it
 * written, so it reads as erased flash (or, on a fused chip, as the garbage an
 * erased sector decrypts to), the magic does not match, and the boot carries
 * on exactly as before.
 *
 * Who may write it (spec S15.2): anyone who can talk to the board's protocol,
 * the paired PC over WiFi or whoever holds the cable (logo_write.c, behind
 * logo_begin / logo_chunk / G frames / logo_end, and logo_remove). That is the
 * OTA image's boundary, and it is weaker than the factory's "esptool with the
 * board in bootloader mode" that this comment used to promise. It is why
 * logo_parse below checks the whole blob and not only its header: what the
 * protocol can write, the boot must be able to refuse.
 *
 * Layout (all little-endian):
 *
 *    0  "BLGO"
 *    4  u16 version        (1)
 *    6  u16 hold_ms        how long the last frame stays up after the clip
 *    8  u32 blob_len       length of the BAN1 blob that follows the header
 *   12  u32 crc32          IEEE CRC-32 of the blob (zlib.crc32 in Python)
 *   16  u8  bg_r, bg_g, bg_b   what the screen is filled with before frame 0
 *   19  reserved, zero, to 32
 *   32  BAN1 blob          same format as the boot clips (bootanim_dec.h);
 *                          one frame for a still logo, more for a clip
 *
 * The CRC is what makes "present" trustworthy: half a write, or a partition
 * from a future tool that laid things out differently, fails it and is
 * treated as absent rather than played as noise.
 */
#define LOGO_HDR_LEN 32
#define LOGO_VERSION 1

/*
 * Playback caps (D60), one definition that pc/logo_encode.py mirrors so the
 * daemon refuses before it sends, and tools/encode_logo.py and burn.sh
 * enforce so the factory cannot burn what the board would refuse. Set at
 * Task 6.3 from every factory logo that exists (2026-11-07): the largest
 * needs 52 frames at 15 fps, hold 1800 ms, 5266 ms in all, 5 rects in a
 * frame. LOGO_MAX_TOTAL_MS is the whole time the boot may spend on a logo,
 * nframes * 1000 / fps + hold_ms. 120 rects is every frame the encoder can
 * produce: it cuts bands of changed rows, and 240 rows give at most 120.
 */
#define LOGO_MAX_FRAMES 180
#define LOGO_FPS_MIN 1
#define LOGO_FPS_MAX 60
#define LOGO_MAX_TOTAL_MS 15000
#define LOGO_MAX_RECTS 120

struct logo_info {
	const uint8_t *blob;
	size_t blob_len;
	uint16_t nframes;
	uint16_t hold_ms;
	uint32_t bg_rgb;
};

/*
 * Validate a logo partition image and describe it. `part` is the start of
 * the partition (memory-mapped in the firmware, a byte array in tests),
 * `part_len` its size. The BAN1 canvas must be exactly want_w x want_h, the
 * panel's size: a logo authored for another screen would be blitted off the
 * edge, so it is refused here instead. Returns false for anything that is
 * not a complete, intact logo -- the caller then boots as an individual unit.
 *
 * Checked: the CRC, the header, the caps above, then a strict PackBits16 walk
 * of every frame: every rect inside the canvas with non-zero size, its runs
 * producing exactly w*h pixels with none crossing that count, the last run
 * ending exactly at the rect's declared payload length, and the walk ending
 * exactly at blob_len. Called at logo_end and at every boot (logo.c), so a
 * bad blob already on the flash is absent too.
 */
bool logo_parse(const uint8_t *part, size_t part_len,
		uint16_t want_w, uint16_t want_h, struct logo_info *out);

/* IEEE CRC-32 (the zlib one), exposed so tests can build fixtures. */
uint32_t logo_crc32(const uint8_t *p, size_t n);

#endif /* LOGO_PARSE_H */
