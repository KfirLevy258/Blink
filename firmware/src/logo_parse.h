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
 * on exactly as before. Nothing over USB can write it -- it takes esptool with
 * the board held in bootloader mode, which is the same boundary the edition
 * stamp has, and unlike the edition it can be undone the same way (erase the
 * region and the unit is an individual one again).
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
 */
bool logo_parse(const uint8_t *part, size_t part_len,
		uint16_t want_w, uint16_t want_h, struct logo_info *out);

/* IEEE CRC-32 (the zlib one), exposed so tests can build fixtures. */
uint32_t logo_crc32(const uint8_t *p, size_t n);

#endif /* LOGO_PARSE_H */
