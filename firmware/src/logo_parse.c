/*
 * BLGO header parser. Pure C -- no Zephyr -- so tests/logo/host_test.c runs
 * it on the dev machine. Format documented in logo_parse.h.
 */
#include <string.h>

#include "logo_parse.h"
#include "bootanim_dec.h"

static uint16_t rd16(const uint8_t *p)
{
	return (uint16_t)(p[0] | (p[1] << 8));
}

static uint32_t rd32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
	       ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* Nibble-table CRC-32: 64 bytes of table instead of 1 KB, and still an
 * order of magnitude faster than bit-at-a-time over a 100 KB clip read
 * through the flash cache at boot. */
uint32_t logo_crc32(const uint8_t *p, size_t n)
{
	static const uint32_t t[16] = {
		0x00000000, 0x1db71064, 0x3b6e20c8, 0x26d930ac,
		0x76dc4190, 0x6b6b51f4, 0x4db26158, 0x5005713c,
		0xedb88320, 0xf00f9344, 0xd6d6a3e8, 0xcb61b38c,
		0x9b64c2b0, 0x86d3d2d4, 0xa00ae278, 0xbdbdf21c,
	};
	uint32_t c = 0xffffffffu;

	while (n--) {
		c ^= *p++;
		c = t[c & 15] ^ (c >> 4);
		c = t[c & 15] ^ (c >> 4);
	}
	return c ^ 0xffffffffu;
}

/*
 * One rect's payload: exactly `count` pixels from exactly `plen` bytes. Not
 * ba_decode_frame: the player clips a run to the pixels it still wants
 * (bootanim_dec.c pb_fill) and then jumps to the declared payload end without
 * looking at what it skipped, which is right for a player and wrong for a
 * validator. This one consumes every run.
 */
static bool walk_payload(const uint8_t *p, uint32_t plen, uint32_t count)
{
	const uint8_t *end = p + plen;
	uint32_t got = 0;

	while (got < count) {
		if (p >= end) {
			return false;
		}
		uint8_t c = *p++;
		uint32_t n = c < 128 ? (uint32_t)c + 1 : (uint32_t)c - 126;
		/* a literal carries n pixels, a run one */
		size_t need = c < 128 ? 2u * n : 2u;

		if ((size_t)(end - p) < need || n > count - got) {
			return false;
		}
		p += need;
		got += n;
	}
	return p == end;
}

bool logo_parse(const uint8_t *part, size_t part_len,
		uint16_t want_w, uint16_t want_h, struct logo_info *out)
{
	if (part == NULL || part_len < LOGO_HDR_LEN + 12)
		return false;
	if (memcmp(part, "BLGO", 4) != 0)
		return false;
	if (rd16(part + 4) != LOGO_VERSION)
		return false;

	uint32_t blob_len = rd32(part + 8);

	/* Bounded BEFORE the CRC walks it: a header claiming 4 GB must not
	 * send the checksum off the end of the partition. */
	if (blob_len < 12 || blob_len > part_len - LOGO_HDR_LEN)
		return false;

	const uint8_t *blob = part + LOGO_HDR_LEN;

	if (logo_crc32(blob, blob_len) != rd32(part + 12))
		return false;

	struct ba_header hdr;
	size_t off;

	if (!ba_parse_header(blob, blob_len, &hdr, &off))
		return false;
	uint16_t hold_ms = rd16(part + 6);

	/* flags 1 is big-endian pixels, the only thing either encoder writes
	 * (pc/logo_encode.py refuses anything else the same way). */
	if (hdr.w != want_w || hdr.h != want_h || hdr.flags != 1)
		return false;
	if (hdr.nframes < 1 || hdr.nframes > LOGO_MAX_FRAMES)
		return false;
	if (hdr.fps < LOGO_FPS_MIN || hdr.fps > LOGO_FPS_MAX)
		return false;
	/* nframes * 1000 / fps + hold_ms <= MAX, in integers: at most
	 * 180 * 1000 + 65535 * 60, well inside 32 bits. */
	if ((uint32_t)hdr.nframes * 1000u + (uint32_t)hold_ms * hdr.fps >
	    (uint32_t)LOGO_MAX_TOTAL_MS * hdr.fps)
		return false;

	size_t o = off;

	for (uint32_t f = 0; f < hdr.nframes; f++) {
		if (o >= blob_len)
			return false;

		unsigned nrects = blob[o++];

		if (nrects > LOGO_MAX_RECTS)
			return false;
		for (unsigned r = 0; r < nrects; r++) {
			if (blob_len - o < 12)
				return false;

			uint32_t x = rd16(blob + o), y = rd16(blob + o + 2);
			uint32_t w = rd16(blob + o + 4), h = rd16(blob + o + 6);
			uint32_t plen = rd32(blob + o + 8);

			o += 12;
			if (w == 0 || h == 0 || x + w > want_w || y + h > want_h)
				return false;
			if (plen > blob_len - o || !walk_payload(blob + o, plen, w * h))
				return false;
			o += plen;
		}
	}
	if (o != blob_len)
		return false;

	out->blob = blob;
	out->blob_len = blob_len;
	out->nframes = hdr.nframes;
	out->hold_ms = hold_ms;
	out->bg_rgb = ((uint32_t)part[16] << 16) | ((uint32_t)part[17] << 8) |
		      part[18];
	return true;
}
