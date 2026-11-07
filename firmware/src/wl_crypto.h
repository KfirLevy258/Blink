/*
 * The only door from the WiFi link to a cipher. wl_hs.c and wl_frame.c call
 * these and nothing else, so their host tests link a deterministic fake
 * (tests/support/wl_crypto_fake.c) and the board links wl_crypto_mbedtls.c
 * (mbedTLS 3.6.6, Zephyr 4.3.1's module, and Zephyr's CSPRNG).
 *
 * The PC side is pc/wifi_crypto.py, pure Python. The two are held to each
 * other at every plaintext length 0..517 by tests/wl_crypto_real (vectors
 * generated from the Python by tools/gen_aead_vectors.py), and the board
 * checks itself against the RFCs at every boot (wl_crypto_selftest).
 *
 * Every function returns 0 on success and a negative number on failure,
 * except wl_ct_equal, which answers a question: 1 for equal, 0 for
 * different. Nothing is void, because mbedtls_md_setup can fail its allocation
 * (hkdf.c:107, md.c) and a caller must never use an output whose call
 * failed. Nothing here prints: a key, nonce or tag never reaches a log.
 */
#ifndef WL_CRYPTO_H
#define WL_CRYPTO_H

#include <stddef.h>
#include <stdint.h>

int wl_hmac_sha256(const uint8_t *key, size_t klen, const uint8_t *msg,
		   size_t mlen, uint8_t out[32]);

/* RFC 5869 extract-and-expand, SHA-256. */
int wl_hkdf_sha256(const uint8_t *salt, size_t sl, const uint8_t *ikm,
		   size_t il, const uint8_t *info, size_t inl, uint8_t *out,
		   size_t ol);

/* RFC 8439 AEAD. `pt` and `ct` may be the same buffer. */
int wl_aead_seal(const uint8_t key[32], const uint8_t nonce[12],
		 const uint8_t *aad, size_t al, const uint8_t *pt, size_t n,
		 uint8_t *ct, uint8_t tag[16]);

/* 0 and the plaintext in `pt`, or -1 for a tag that does not verify or any
 * other failure. On -1 not one byte of `pt` has been written: the tag is
 * checked before anything is decrypted, as pc/wifi_crypto.aead_open does, so
 * no plaintext of a forged frame ever exists. `ct` and `pt` may be the same
 * buffer. */
int wl_aead_open(const uint8_t key[32], const uint8_t nonce[12],
		 const uint8_t *aad, size_t al, const uint8_t *ct, size_t n,
		 const uint8_t tag[16], uint8_t *pt);

/* sys_csrand_get. A failure is reported, never papered over: a repeated
 * board nonce would let a captured M2 replay (spec S5.4). */
int wl_random(uint8_t *out, size_t n);

/* 1 when equal, 0 when not; constant time in n whatever the contents
 * (mbedtls_ct_memcmp). */
int wl_ct_equal(const uint8_t *a, const uint8_t *b, size_t n);

/* One streaming SHA-256 at a time (the OTA image's, ota_net.c): start, update
 * in any chunking, finish into out[32]. A single module-level context rather
 * than a caller's, so this header needs no mbedTLS type and the host tests
 * stay free of it. start discards a hash left unfinished by an aborted
 * stream; software SHA-256 holds no allocation, so there is nothing to free. */
int wl_sha256_start(void);
int wl_sha256_update(const uint8_t *p, size_t n);
int wl_sha256_finish(uint8_t out[32]);

/* 0 when this build's cipher reproduces RFC 8439 section 2.8.2 (seal, open,
 * and a refused flipped tag) and RFC 5869 test case 1 (its PRK through
 * wl_hmac_sha256, its OKM through wl_hkdf_sha256); -1 otherwise. main.c runs
 * it at boot, before the network thread exists, and a board that fails it
 * never brings the radio up. */
int wl_crypto_selftest(void);

#endif /* WL_CRYPTO_H */
