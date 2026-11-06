/*
 * Spec S6.2 and S6.4, byte for byte; see wl_hs.h for the layouts. Pure: the
 * only calls out are wl_crypto.h, and nothing here prints, so a failure is
 * the caller's to log with a fixed string (spec S5.8).
 *
 * Every HMAC input is assembled in one fixed stack buffer. The longest is
 * "blink-wifi-1 board" || Nb || Np, 18 + 16 + 16 = 50 bytes.
 */
#include <stdbool.h>
#include <string.h>

#include "wl_crypto.h"
#include "wl_hs.h"

#define HMAC_IN_MAX 50

static const uint8_t M1_MAGIC[4] = { 'B', 'L', 'W', 0x01 };
static const uint8_t Q_MAGIC[4] = { 'B', 'L', 'Q', 0x01 };
static const uint8_t A_MAGIC[4] = { 'B', 'L', 'A', 0x01 };

#define LBL_PC "blink-wifi-1 pc"
#define LBL_BOARD "blink-wifi-1 board"
#define LBL_SESSION "blink-wifi-1 session"
#define LBL_Q "blink-disc-1 q"
#define LBL_A "blink-disc-1 a"

/* A zeroing the compiler may not drop as a dead store. */
static void wipe(void *p, size_t n)
{
	volatile uint8_t *v = p;

	while (n--) {
		*v++ = 0;
	}
}

/* HMAC(K, label || a[an] || b[bn]). 0 or -2. */
static int tag_of(const uint8_t key[32], const char *label, const uint8_t *a,
		  size_t an, const uint8_t *b, size_t bn, uint8_t out[32])
{
	uint8_t in[HMAC_IN_MAX];
	size_t ln = strlen(label);
	int rc;

	if (ln + an + bn > sizeof(in)) {
		return -2;
	}
	memcpy(in, label, ln);
	memcpy(in + ln, a, an);
	if (bn) {
		memcpy(in + ln + an, b, bn);
	}
	rc = wl_hmac_sha256(key, 32, in, ln + an + bn, out) == 0 ? 0 : -2;
	wipe(in, sizeof(in));
	return rc;
}

int wl_hs_m1(struct wl_hs *hs, uint8_t out[WL_M1_LEN])
{
	wipe(hs, sizeof(*hs));
	if (wl_random(hs->nb, sizeof(hs->nb)) != 0) {
		wipe(hs, sizeof(*hs));
		return -1;
	}
	memcpy(out, M1_MAGIC, sizeof(M1_MAGIC));
	memcpy(out + sizeof(M1_MAGIC), hs->nb, sizeof(hs->nb));
	return 0;
}

int wl_hs_check_m2(struct wl_hs *hs, const uint8_t key[32], const uint8_t *m2,
		   size_t n)
{
	uint8_t want[32];
	int rc;

	if (n != WL_M2_LEN) {
		return -1;
	}
	/* m2 = Np[16] || tag[32]; the tag covers this session's Nb, so an M2
	 * from an earlier session (another Nb) and the board's own M3 (another
	 * label) both fail here. */
	if (tag_of(key, LBL_PC, hs->nb, 16, m2, 16, want) != 0) {
		wipe(want, sizeof(want));
		return -2;
	}
	rc = wl_ct_equal(want, m2 + 16, 32) ? 0 : -1;
	wipe(want, sizeof(want));
	if (rc == 0) {
		memcpy(hs->np, m2, 16);
	}
	return rc;
}

int wl_hs_m3(const struct wl_hs *hs, const uint8_t key[32],
	     uint8_t out[WL_M3_LEN])
{
	if (tag_of(key, LBL_BOARD, hs->nb, 16, hs->np, 16, out) != 0) {
		wipe(out, WL_M3_LEN);
		return -2;
	}
	return 0;
}

int wl_hs_keys(const struct wl_hs *hs, const uint8_t key[32],
	       uint8_t k_pb[32], uint8_t k_bp[32])
{
	uint8_t salt[32];
	uint8_t keys[64];
	int rc;

	memcpy(salt, hs->nb, 16);
	memcpy(salt + 16, hs->np, 16);
	rc = wl_hkdf_sha256(salt, sizeof(salt), key, 32,
			    (const uint8_t *)LBL_SESSION, strlen(LBL_SESSION),
			    keys, sizeof(keys));
	if (rc == 0) {
		memcpy(k_pb, keys, 32);
		memcpy(k_bp, keys + 32, 32);
	} else {
		wipe(k_pb, 32);
		wipe(k_bp, 32);
		rc = -2;
	}
	wipe(keys, sizeof(keys));
	wipe(salt, sizeof(salt));
	return rc;
}

void wl_hs_wipe(struct wl_hs *hs)
{
	wipe(hs, sizeof(*hs));
}

int wl_disc_answer(const uint8_t key[32], const uint8_t *q, size_t n,
		   uint16_t port, uint8_t out[WL_A_LEN])
{
	uint8_t t[32];
	uint8_t pb[2] = { (uint8_t)(port >> 8), (uint8_t)port };
	bool ok;

	if (n != WL_Q_LEN || memcmp(q, Q_MAGIC, sizeof(Q_MAGIC)) != 0) {
		return -1;
	}
	if (tag_of(key, LBL_Q, q + 4, 16, NULL, 0, t) != 0) {
		wipe(t, sizeof(t));
		return -1;
	}
	ok = wl_ct_equal(t, q + 20, 16);
	if (!ok || tag_of(key, LBL_A, q + 4, 16, pb, 2, t) != 0) {
		wipe(t, sizeof(t));
		return -1;
	}
	memcpy(out, A_MAGIC, sizeof(A_MAGIC));
	memcpy(out + 4, t, 16);
	memcpy(out + 20, pb, 2);
	wipe(t, sizeof(t));
	return WL_A_LEN;
}
