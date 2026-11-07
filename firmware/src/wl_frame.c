/*
 * Spec S6.3; see wl_frame.h for the layout. Pure: wl_crypto.h only, no
 * printk. A direction that fails once stays dead: a session whose counters
 * or keys are in doubt is closed, never resynchronised (spec S5.4).
 */
#include <string.h>

#include "wl_crypto.h"
#include "wl_frame.h"

static void wipe(void *p, size_t n)
{
	volatile uint8_t *v = p;

	while (n--) {
		*v++ = 0;
	}
}

static void nonce_of(uint64_t ctr, uint8_t nonce[WL_NONCE_LEN])
{
	memset(nonce, 0, 4);
	for (int i = 0; i < 8; i++) {
		nonce[4 + i] = (uint8_t)(ctr >> (56 - 8 * i));
	}
}

static int die(struct wl_dir *d)
{
	d->dead = true;
	return -1;
}

/* Any type byte is sealed, by design: the callers pass L, O or G and the
 * reader (wl_frame_open) is where an unknown one is refused. */
int wl_frame_seal(struct wl_dir *d, uint8_t type, const uint8_t *body,
		  size_t n, uint8_t *out, size_t out_max)
{
	uint8_t nonce[WL_NONCE_LEN];
	size_t len = 1 + n;

	/* At 2^64 - 1 the next nonce would repeat; at one frame a millisecond
	 * that is 584 million years away, and the check costs nothing. */
	/* n is checked alone and first: 1 + n wraps at SIZE_MAX. */
	if (d->dead || n > WL_PT_MAX - 1 || len > WL_PT_MAX || out_max < 2 + len + WL_TAG_LEN ||
	    d->ctr == UINT64_MAX) {
		return die(d);
	}
	out[0] = (uint8_t)(len >> 8);
	out[1] = (uint8_t)len;
	out[2] = type;
	if (n) {
		memcpy(out + 3, body, n);
	}
	nonce_of(d->ctr, nonce);
	if (wl_aead_seal(d->key, nonce, out, 2, out + 2, len, out + 2,
			 out + 2 + len) != 0) {
		wipe(out, out_max);
		return die(d);
	}
	d->ctr++;
	return (int)(2 + len + WL_TAG_LEN);
}

int wl_frame_len(const uint8_t hdr[2])
{
	int len = (hdr[0] << 8) | hdr[1];

	return (len == 0 || len > WL_PT_MAX) ? -1 : len;
}

int wl_frame_open(struct wl_dir *d, const uint8_t *frame, size_t n,
		  uint8_t *type, uint8_t *body, size_t body_max)
{
	uint8_t nonce[WL_NONCE_LEN];
	uint8_t pt[WL_PT_MAX];
	int len;

	if (d->dead || n < 2 || d->ctr == UINT64_MAX) {
		return die(d);
	}
	len = wl_frame_len(frame);
	if (len < 0 || n != (size_t)(2 + len + WL_TAG_LEN) ||
	    body_max < (size_t)(len - 1)) {
		return die(d);
	}
	nonce_of(d->ctr, nonce);
	if (wl_aead_open(d->key, nonce, frame, 2, frame + 2, (size_t)len,
			 frame + 2 + len, pt) != 0) {
		wipe(pt, sizeof(pt));
		return die(d);
	}
	if (pt[0] != WL_T_LINE && pt[0] != WL_T_OTA && pt[0] != WL_T_LOGO) {
		wipe(pt, sizeof(pt));
		return die(d);
	}
	*type = pt[0];
	memcpy(body, pt + 1, (size_t)(len - 1));
	wipe(pt, sizeof(pt));
	d->ctr++;
	return len - 1;
}

void wl_dir_wipe(struct wl_dir *d)
{
	wipe(d, sizeof(*d));
	/* An all-zero key must never be live: a wiped direction stays dead. */
	d->dead = true;
}
