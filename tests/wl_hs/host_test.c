/* firmware/src/wl_hs.c, unchanged, on the toy cipher of
 * tests/support/wl_crypto_fake.c: what the handshake does with the cipher's
 * answers -- lengths, which bytes it hashes in which order, wiping, and every
 * error path. That the real HMAC and HKDF give the PC's bytes is
 * tests/wl_wire_real. Silence is a result too: a discovery query that is not
 * ours must produce no answer at all (-1), never a short or zeroed one.
 */
#include <stdio.h>
#include <string.h>

#include "wl_crypto.h"
#include "wl_hs.h"

extern int wl_fake_fail_next;

static int fails;
static void check(int ok, const char *name)
{
	if (!ok) {
		fails++;
		printf("FAIL %s\n", name);
	} else {
		printf("PASS %s\n", name);
	}
}

static const uint8_t K[32] = { 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
			       15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26,
			       27, 28, 29, 30, 31, 32 };
static const uint8_t K2[32] = { 9 };

static void mac(const uint8_t key[32], const char *label, const uint8_t *a,
		size_t an, const uint8_t *b, size_t bn, uint8_t out[32])
{
	uint8_t in[64];
	size_t l = strlen(label);

	memcpy(in, label, l);
	memcpy(in + l, a, an);
	if (bn) {
		memcpy(in + l + an, b, bn);
	}
	wl_hmac_sha256(key, 32, in, l + an + bn, out);
}

static int all_zero(const uint8_t *p, size_t n)
{
	while (n--) {
		if (*p++) {
			return 0;
		}
	}
	return 1;
}

/* A PC's M2 for hs's Nb under key. */
static void make_m2(const struct wl_hs *hs, const uint8_t key[32], uint8_t np0,
		    uint8_t m2[WL_M2_LEN])
{
	memset(m2, np0, 16);
	mac(key, "blink-wifi-1 pc", hs->nb, 16, m2, 16, m2 + 16);
}

static void test_hs(void)
{
	struct wl_hs hs, hs2;
	uint8_t m1[WL_M1_LEN], m1b[WL_M1_LEN], m2[WL_M2_LEN + 1], want[32];
	uint8_t m3[WL_M3_LEN], kpb[32], kbp[32];

	memset(&hs, 0xee, sizeof(hs));
	check(wl_hs_m1(&hs, m1) == 0 && memcmp(m1, "BLW\x01", 4) == 0 &&
		      memcmp(m1 + 4, hs.nb, 16) == 0,
	      "m1_layout");
	check(wl_hs_m1(&hs2, m1b) == 0 && memcmp(m1, m1b, WL_M1_LEN) != 0,
	      "m1_fresh_per_call");
	wl_fake_fail_next = 1;
	memset(m1b, 0x77, sizeof(m1b));
	check(wl_hs_m1(&hs2, m1b) == -1 && all_zero(hs2.nb, 16) &&
		      m1b[0] == 0x77,
	      "m1_fails_without_entropy");

	wl_hs_m1(&hs, m1);
	make_m2(&hs, K, 0x42, m2);
	check(wl_hs_check_m2(&hs, K, m2, WL_M2_LEN) == 0 && hs.np[0] == 0x42 &&
		      hs.np[15] == 0x42,
	      "m2_good_accepted");

	memset(hs.np, 0, 16);
	m2[47] ^= 1;	/* the LAST tag byte: the whole 32 are compared */
	check(wl_hs_check_m2(&hs, K, m2, WL_M2_LEN) == -1 && all_zero(hs.np, 16),
	      "m2_last_tag_byte_checked");
	m2[47] ^= 1;
	memset(hs.np, 0, 16);
	check(wl_hs_check_m2(&hs, K, m2, 47) == -1 && all_zero(hs.np, 16),
	      "m2_short_refused");
	m2[48] = 0;
	check(wl_hs_check_m2(&hs, K, m2, 49) == -1 && all_zero(hs.np, 16),
	      "m2_long_refused");
	check(wl_hs_check_m2(&hs, K2, m2, WL_M2_LEN) == -1 &&
		      all_zero(hs.np, 16),
	      "m2_wrong_key_refused");

	/* The board's own M3 is 32 bytes of the same key over the same Nb; a
	 * 48-byte message built from it must not pass as an M2. */
	{
		uint8_t refl[WL_M2_LEN];

		memset(hs.np, 0x42, 16);
		wl_hs_m3(&hs, K, m3);
		memset(refl, 0x42, 16);
		memcpy(refl + 16, m3, 32);
		memset(hs.np, 0, 16);
		check(wl_hs_check_m2(&hs, K, refl, WL_M2_LEN) == -1,
		      "m2_reflected_board_tag_refused");
	}
	{
		struct wl_hs old = hs;
		uint8_t old_m2[WL_M2_LEN];

		wl_hs_m1(&old, m1b);
		make_m2(&old, K, 0x11, old_m2);
		wl_hs_m1(&hs, m1);	/* a new session, another Nb */
		check(wl_hs_check_m2(&hs, K, old_m2, WL_M2_LEN) == -1,
		      "m2_replayed_from_last_session_refused");
	}

	/* A crypto failure is -2, not "bad tag". */
	make_m2(&hs, K, 0x42, m2);
	wl_fake_fail_next = 1;
	check(wl_hs_check_m2(&hs, K, m2, WL_M2_LEN) == -2,
	      "m2_reports_crypto_failure");
	check(wl_hs_check_m2(&hs, K, m2, WL_M2_LEN) == 0, "m2_after_failure_ok");

	/* M3 and the keys hash what the spec says, in the spec's order. */
	mac(K, "blink-wifi-1 board", hs.nb, 16, hs.np, 16, want);
	check(wl_hs_m3(&hs, K, m3) == 0 && memcmp(m3, want, 32) == 0,
	      "m3_is_label_nb_np");
	memset(m3, 0x55, sizeof(m3));
	wl_fake_fail_next = 1;
	check(wl_hs_m3(&hs, K, m3) == -2 && all_zero(m3, sizeof(m3)),
	      "m3_reports_crypto_failure");

	{
		uint8_t salt[32], keys[64];

		memcpy(salt, hs.nb, 16);
		memcpy(salt + 16, hs.np, 16);
		wl_hkdf_sha256(salt, 32, K, 32,
			       (const uint8_t *)"blink-wifi-1 session", 20,
			       keys, 64);
		check(wl_hs_keys(&hs, K, kpb, kbp) == 0 &&
			      memcmp(kpb, keys, 32) == 0 &&
			      memcmp(kbp, keys + 32, 32) == 0,
		      "keys_are_hkdf_halves_pc_to_board_first");
		check(memcmp(kpb, kbp, 32) != 0, "keys_differ_per_direction");
	}
	memset(kpb, 0x55, 32);
	memset(kbp, 0x55, 32);
	wl_fake_fail_next = 1;
	check(wl_hs_keys(&hs, K, kpb, kbp) == -2 && all_zero(kpb, 32) &&
		      all_zero(kbp, 32),
	      "keys_report_crypto_failure");

	wl_hs_wipe(&hs);
	check(all_zero((const uint8_t *)&hs, sizeof(hs)), "wipe_zeroes");
}

static void make_q(const uint8_t key[32], uint8_t q[WL_Q_LEN])
{
	uint8_t t[32];

	memcpy(q, "BLQ\x01", 4);
	for (int i = 0; i < 16; i++) {
		q[4 + i] = (uint8_t)(0xa0 + i);
	}
	mac(key, "blink-disc-1 q", q + 4, 16, NULL, 0, t);
	memcpy(q + 20, t, 16);
}

static void test_disc(void)
{
	uint8_t q[WL_Q_LEN + 1], a[WL_A_LEN], t[32], pb[2] = { 0xc2, 0x43 };

	make_q(K, q);
	memset(a, 0, sizeof(a));
	check(wl_disc_answer(K, q, WL_Q_LEN, 49731, a) == WL_A_LEN &&
		      memcmp(a, "BLA\x01", 4) == 0 && a[20] == 0xc2 &&
		      a[21] == 0x43,
	      "disc_good_answered");
	mac(K, "blink-disc-1 a", q + 4, 16, pb, 2, t);
	check(memcmp(a + 4, t, 16) == 0, "disc_answer_tag_is_label_nq_port");

	memset(a, 0x77, sizeof(a));
	q[36] = 0;
	check(wl_disc_answer(K, q, 35, 49731, a) == -1 &&
		      wl_disc_answer(K, q, 37, 49731, a) == -1 && a[0] == 0x77,
	      "disc_wrong_len_silent");
	q[20] ^= 1;
	check(wl_disc_answer(K, q, WL_Q_LEN, 49731, a) == -1 && a[0] == 0x77,
	      "disc_bad_tag_silent");
	make_q(K, q);
	q[2] = 'X';
	check(wl_disc_answer(K, q, WL_Q_LEN, 49731, a) == -1 && a[0] == 0x77,
	      "disc_wrong_magic_silent");
	make_q(K, q);
	q[3] = 2;
	check(wl_disc_answer(K, q, WL_Q_LEN, 49731, a) == -1 && a[0] == 0x77,
	      "disc_wrong_version_silent");
	make_q(K, q);
	q[35] ^= 1;
	check(wl_disc_answer(K, q, WL_Q_LEN, 49731, a) == -1 && a[0] == 0x77,
	      "disc_last_tag_byte_checked");
	make_q(K2, q);
	check(wl_disc_answer(K, q, WL_Q_LEN, 49731, a) == -1,
	      "disc_wrong_key_silent");
	make_q(K, q);
	wl_fake_fail_next = 1;
	check(wl_disc_answer(K, q, WL_Q_LEN, 49731, a) == -1 && a[0] == 0x77,
	      "disc_crypto_failure_silent");
}

static void test_ct(void)
{
	uint8_t a[4] = { 1, 2, 3, 4 }, b[4] = { 1, 2, 3, 4 };

	check(wl_ct_equal(a, b, 4) == 1, "ct_equal_basic");
	b[3] ^= 0x80;
	check(wl_ct_equal(a, b, 4) == 0, "ct_unequal_basic");
}

int main(void)
{
	test_hs();
	test_disc();
	test_ct();
	return fails ? 1 : 0;
}
