#include <string.h>

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

bool ba_parse_header(const uint8_t *blob, size_t len,
		     struct ba_header *hdr, size_t *off)
{
	if (len < 12 || memcmp(blob, "BAN1", 4) != 0)
		return false;
	hdr->w = rd16(blob + 4);
	hdr->h = rd16(blob + 6);
	hdr->fps = blob[8];
	hdr->flags = blob[9];
	hdr->nframes = rd16(blob + 10);
	if (hdr->fps == 0 || hdr->w == 0 || hdr->h == 0)
		return false;
	*off = 12;
	return true;
}

/* PackBits16 reader whose active run/literal survives strip boundaries:
 * one RLE op frequently covers far more pixels than one strip holds. */
struct pb {
	const uint8_t *p, *end;
	size_t rem;		/* pixels left in the current op */
	bool is_run;
	uint8_t run_hi, run_lo;
};

static bool pb_fill(struct pb *s, uint8_t *dst, size_t npix)
{
	while (npix > 0) {
		if (s->rem == 0) {
			if (s->p >= s->end)
				return false;
			uint8_t c = *s->p++;

			if (c < 128) {
				s->is_run = false;
				s->rem = (size_t)c + 1;
			} else {
				s->is_run = true;
				s->rem = (size_t)c - 126;
				if ((size_t)(s->end - s->p) < 2)
					return false;
				s->run_hi = s->p[0];
				s->run_lo = s->p[1];
				s->p += 2;
			}
		}

		size_t n = s->rem < npix ? s->rem : npix;

		if (s->is_run) {
			for (size_t i = 0; i < n; i++) {
				*dst++ = s->run_hi;
				*dst++ = s->run_lo;
			}
		} else {
			if ((size_t)(s->end - s->p) < 2 * n)
				return false;
			memcpy(dst, s->p, 2 * n);
			s->p += 2 * n;
			dst += 2 * n;
		}
		s->rem -= n;
		npix -= n;
	}
	return true;
}

int ba_decode_frame(const uint8_t *blob, size_t len, size_t *off,
		    uint8_t *strip, size_t strip_bytes,
		    ba_blit_fn blit, void *user)
{
	size_t o = *off;

	if (o >= len)
		return -1;

	int nrects = blob[o++];

	for (int r = 0; r < nrects; r++) {
		if (len - o < 12)
			return -1;

		uint16_t x = rd16(blob + o), y = rd16(blob + o + 2);
		uint16_t w = rd16(blob + o + 4), h = rd16(blob + o + 6);
		uint32_t plen = rd32(blob + o + 8);

		o += 12;
		if (w == 0 || plen > len - o)
			return -1;

		struct pb s = { .p = blob + o, .end = blob + o + plen };
		uint16_t rows_per = strip_bytes / (2u * w);

		if (rows_per == 0)
			return -1;	/* strip buffer narrower than rect */

		for (uint16_t ry = 0; ry < h; ry += rows_per) {
			uint16_t rows = (uint16_t)(h - ry) < rows_per
					? (uint16_t)(h - ry) : rows_per;

			if (!pb_fill(&s, strip, (size_t)rows * w))
				return -1;
			blit(x, y + ry, w, rows, strip, user);
		}
		o += plen;
	}
	*off = o;
	return nrects;
}
