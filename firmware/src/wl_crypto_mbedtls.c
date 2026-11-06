/*
 * wl_crypto.h on mbedTLS 3.6.6 (Zephyr 4.3.1's module) and Zephyr's CSPRNG.
 *
 * Started from the Task 0.2 spike's version, with two changes the spike did
 * not need and the real link does:
 *
 * - wl_aead_open checks the tag BEFORE it decrypts. mbedtls_chachapoly_
 *   auth_decrypt decrypts into the caller's buffer first and zeroes it on a
 *   bad tag (chachapoly.c:304-315), so for that moment a forged frame's
 *   plaintext exists, and afterwards the buffer holds zeros instead of what
 *   it held. pc/wifi_crypto.aead_open never produces a forged plaintext at
 *   all (spec S7.4), and the board now does the same: the tag is computed
 *   over the ciphertext through a 64-byte scratch block, compared in
 *   constant time, and only a frame that verified is decrypted into `pt`.
 *   tests/wl_crypto_real requires every corrupted case to leave `pt`
 *   untouched.
 *
 * - every failure is passed up. mbedTLS allocates from the kernel heap
 *   (main.c points its calloc at k_calloc), and mbedtls_md_setup can fail
 *   that allocation inside HMAC and HKDF (hkdf.c:107).
 *
 * The boot self-test is here too, beside the code it tests, so a build that
 * links this file cannot leave it out.
 */
#include <string.h>

#include <zephyr/random/random.h>
#include <mbedtls/md.h>
#include <mbedtls/hkdf.h>
#include <mbedtls/chacha20.h>
#include <mbedtls/chachapoly.h>
#include <mbedtls/constant_time.h>
#include <mbedtls/platform_util.h>

#include "wl_crypto.h"

static const mbedtls_md_info_t *sha256(void)
{
	return mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
}

int wl_hmac_sha256(const uint8_t *key, size_t klen, const uint8_t *msg,
		   size_t mlen, uint8_t out[32])
{
	const mbedtls_md_info_t *md = sha256();

	if (md == NULL) {
		return -1;
	}
	return mbedtls_md_hmac(md, key, klen, msg, mlen, out) == 0 ? 0 : -1;
}

int wl_hkdf_sha256(const uint8_t *salt, size_t sl, const uint8_t *ikm,
		   size_t il, const uint8_t *info, size_t inl, uint8_t *out,
		   size_t ol)
{
	const mbedtls_md_info_t *md = sha256();

	if (md == NULL) {
		return -1;
	}
	return mbedtls_hkdf(md, salt, sl, ikm, il, info, inl, out, ol) == 0
		? 0 : -1;
}

int wl_aead_seal(const uint8_t key[32], const uint8_t nonce[12],
		 const uint8_t *aad, size_t al, const uint8_t *pt, size_t n,
		 uint8_t *ct, uint8_t tag[16])
{
	mbedtls_chachapoly_context c;
	int rc;

	mbedtls_chachapoly_init(&c);
	rc = mbedtls_chachapoly_setkey(&c, key);
	if (rc == 0) {
		rc = mbedtls_chachapoly_encrypt_and_tag(&c, n, nonce, aad, al,
							pt, ct, tag);
	}
	mbedtls_chachapoly_free(&c);	/* zeroizes the key schedule */
	return rc == 0 ? 0 : -1;
}

int wl_aead_open(const uint8_t key[32], const uint8_t nonce[12],
		 const uint8_t *aad, size_t al, const uint8_t *ct, size_t n,
		 const uint8_t tag[16], uint8_t *pt)
{
	mbedtls_chachapoly_context c;
	uint8_t scratch[64];
	uint8_t want[16];
	int rc;

	/* Pass 1: the tag. In DECRYPT mode Poly1305 runs over the input, the
	 * ciphertext; the keystream XOR lands in `scratch` and is thrown away. */
	mbedtls_chachapoly_init(&c);
	rc = mbedtls_chachapoly_setkey(&c, key);
	if (rc == 0) {
		rc = mbedtls_chachapoly_starts(&c, nonce,
					       MBEDTLS_CHACHAPOLY_DECRYPT);
	}
	if (rc == 0) {
		rc = mbedtls_chachapoly_update_aad(&c, aad, al);
	}
	for (size_t off = 0; rc == 0 && off < n; off += sizeof(scratch)) {
		size_t k = n - off < sizeof(scratch) ? n - off : sizeof(scratch);

		rc = mbedtls_chachapoly_update(&c, k, ct + off, scratch);
	}
	if (rc == 0) {
		rc = mbedtls_chachapoly_finish(&c, want);
	}
	mbedtls_chachapoly_free(&c);
	mbedtls_platform_zeroize(scratch, sizeof(scratch));

	if (rc == 0 && mbedtls_ct_memcmp(want, tag, sizeof(want)) != 0) {
		rc = -1;
	}
	mbedtls_platform_zeroize(want, sizeof(want));
	if (rc != 0) {
		return -1;
	}

	/* Pass 2: verified, so decrypt. RFC 8439 2.8: the payload's keystream
	 * starts at block counter 1 (block 0 made the Poly1305 key). */
	return mbedtls_chacha20_crypt(key, nonce, 1, n, ct, pt) == 0 ? 0 : -1;
}

int wl_random(uint8_t *out, size_t n)
{
	return sys_csrand_get(out, n) == 0 ? 0 : -1;
}

int wl_ct_equal(const uint8_t *a, const uint8_t *b, size_t n)
{
	return mbedtls_ct_memcmp(a, b, n) == 0;
}

/* ---- the boot self-test ---------------------------------------------------
 *
 * Known answers from the RFCs themselves, the same ones
 * tests/pc/test_wifi_crypto.py holds the PC to. A build whose mbedTLS
 * configuration, port or compiler got any of this wrong would otherwise fail
 * as "the handshake never succeeds", indistinguishable from a wrong key;
 * this makes it one fixed line, [wifi] selftest failed, and keeps the radio
 * off (spec S5.4, D47). */

/* RFC 8439 section 2.8.2. */
static const uint8_t rfc8439_nonce[12] = {
	0x07, 0x00, 0x00, 0x00, 0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47,
};
static const uint8_t rfc8439_aad[12] = {
	0x50, 0x51, 0x52, 0x53, 0xc0, 0xc1, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7,
};
static const char rfc8439_pt[] =
	"Ladies and Gentlemen of the class of '99: If I could offer you only "
	"one tip for the future, sunscreen would be it.";
#define RFC8439_N (sizeof(rfc8439_pt) - 1)
static const uint8_t rfc8439_ct[114] = {
	0xd3, 0x1a, 0x8d, 0x34, 0x64, 0x8e, 0x60, 0xdb, 0x7b, 0x86, 0xaf, 0xbc,
	0x53, 0xef, 0x7e, 0xc2, 0xa4, 0xad, 0xed, 0x51, 0x29, 0x6e, 0x08, 0xfe,
	0xa9, 0xe2, 0xb5, 0xa7, 0x36, 0xee, 0x62, 0xd6, 0x3d, 0xbe, 0xa4, 0x5e,
	0x8c, 0xa9, 0x67, 0x12, 0x82, 0xfa, 0xfb, 0x69, 0xda, 0x92, 0x72, 0x8b,
	0x1a, 0x71, 0xde, 0x0a, 0x9e, 0x06, 0x0b, 0x29, 0x05, 0xd6, 0xa5, 0xb6,
	0x7e, 0xcd, 0x3b, 0x36, 0x92, 0xdd, 0xbd, 0x7f, 0x2d, 0x77, 0x8b, 0x8c,
	0x98, 0x03, 0xae, 0xe3, 0x28, 0x09, 0x1b, 0x58, 0xfa, 0xb3, 0x24, 0xe4,
	0xfa, 0xd6, 0x75, 0x94, 0x55, 0x85, 0x80, 0x8b, 0x48, 0x31, 0xd7, 0xbc,
	0x3f, 0xf4, 0xde, 0xf0, 0x8e, 0x4b, 0x7a, 0x9d, 0xe5, 0x76, 0xd2, 0x65,
	0x86, 0xce, 0xc6, 0x4b, 0x61, 0x16,
};
static const uint8_t rfc8439_tag[16] = {
	0x1a, 0xe1, 0x0b, 0x59, 0x4f, 0x09, 0xe2, 0x6a, 0x7e, 0x90, 0x2e, 0xcb,
	0xd0, 0x60, 0x06, 0x91,
};

/* RFC 5869 appendix A.1, test case 1. */
static const uint8_t rfc5869_prk[32] = {
	0x07, 0x77, 0x09, 0x36, 0x2c, 0x2e, 0x32, 0xdf, 0x0d, 0xdc, 0x3f, 0x0d,
	0xc4, 0x7b, 0xba, 0x63, 0x90, 0xb6, 0xc7, 0x3b, 0xb5, 0x0f, 0x9c, 0x31,
	0x22, 0xec, 0x84, 0x4a, 0xd7, 0xc2, 0xb3, 0xe5,
};
static const uint8_t rfc5869_okm[42] = {
	0x3c, 0xb2, 0x5f, 0x25, 0xfa, 0xac, 0xd5, 0x7a, 0x90, 0x43, 0x4f, 0x64,
	0xd0, 0x36, 0x2f, 0x2a, 0x2d, 0x2d, 0x0a, 0x90, 0xcf, 0x1a, 0x5a, 0x4c,
	0x5d, 0xb0, 0x2d, 0x56, 0xec, 0xc4, 0xc5, 0xbf, 0x34, 0x00, 0x72, 0x08,
	0xd5, 0xb8, 0x87, 0x18, 0x58, 0x65,
};

static int selftest_aead(void)
{
	uint8_t key[32], ct[RFC8439_N], pt[RFC8439_N], tag[16];

	for (int i = 0; i < 32; i++) {
		key[i] = (uint8_t)(0x80 + i);
	}
	if (wl_aead_seal(key, rfc8439_nonce, rfc8439_aad, sizeof(rfc8439_aad),
			 (const uint8_t *)rfc8439_pt, RFC8439_N, ct, tag) != 0 ||
	    memcmp(ct, rfc8439_ct, RFC8439_N) != 0 ||
	    memcmp(tag, rfc8439_tag, 16) != 0) {
		return -1;
	}
	if (wl_aead_open(key, rfc8439_nonce, rfc8439_aad, sizeof(rfc8439_aad),
			 rfc8439_ct, RFC8439_N, rfc8439_tag, pt) != 0 ||
	    memcmp(pt, rfc8439_pt, RFC8439_N) != 0) {
		return -1;
	}
	/* The property that matters most: a tag one bit off is refused. */
	tag[0] ^= 0x01;
	if (wl_aead_open(key, rfc8439_nonce, rfc8439_aad, sizeof(rfc8439_aad),
			 rfc8439_ct, RFC8439_N, tag, pt) != -1) {
		return -1;
	}
	return 0;
}

static int selftest_hkdf(void)
{
	uint8_t ikm[22], salt[13], info[10], prk[32], okm[42];

	memset(ikm, 0x0b, sizeof(ikm));
	for (int i = 0; i < 13; i++) {
		salt[i] = (uint8_t)i;
	}
	for (int i = 0; i < 10; i++) {
		info[i] = (uint8_t)(0xf0 + i);
	}
	/* HKDF-Extract is HMAC(salt, IKM): wl_hmac_sha256 with a 13-byte key. */
	if (wl_hmac_sha256(salt, sizeof(salt), ikm, sizeof(ikm), prk) != 0 ||
	    memcmp(prk, rfc5869_prk, 32) != 0) {
		return -1;
	}
	if (wl_hkdf_sha256(salt, sizeof(salt), ikm, sizeof(ikm), info,
			   sizeof(info), okm, sizeof(okm)) != 0 ||
	    memcmp(okm, rfc5869_okm, sizeof(okm)) != 0) {
		return -1;
	}
	return 0;
}

int wl_crypto_selftest(void)
{
	return selftest_aead() == 0 && selftest_hkdf() == 0 ? 0 : -1;
}
