/* firmware/src/wl_crypto.h for host tests that are not about the cipher.
 *
 * wl_hs and wl_frame (and later ota_net) are tested for what they do with
 * the cipher's answers -- lengths, counters, wiping, error paths -- not for
 * the cipher, which tests/wl_crypto_real holds to the PC byte for byte. So
 * this needs no mbedTLS: a toy keyed hash (FNV-1a, 64-bit, expanded by
 * block index) that is deterministic and changes with every input bit,
 * which is all a "wrong key", "tampered tag" or "replayed frame" test needs.
 * NOT a cipher. Never link it into firmware.
 *
 * wl_fake_fail_next: set it, and the next call (any function but
 * wl_ct_equal, which cannot fail) fails and clears it. That is the path
 * mbedtls_md_setup's allocation failure takes on the board (hkdf.c:107).
 * It returns -ENOMEM, except wl_aead_open and wl_crypto_selftest, whose
 * contracts are 0 or -1.
 */
#include <errno.h>
#include <string.h>

#include "wl_crypto.h"

int wl_fake_fail_next;

static int failing(void)
{
	if (wl_fake_fail_next) {
		wl_fake_fail_next = 0;
		return 1;
	}
	return 0;
}

struct fnv { uint64_t h; };

static void fnv_add(struct fnv *f, const uint8_t *p, size_t n)
{
	for (size_t i = 0; i < n; i++) {
		f->h ^= p[i];
		f->h *= 0x100000001b3ULL;
	}
}

static void fnv_len(struct fnv *f, size_t n)
{
	uint8_t b[8];

	for (int i = 0; i < 8; i++) {
		b[i] = (uint8_t)(n >> (8 * i));
	}
	fnv_add(f, b, 8);
}

/* out[0..ol) = H(domain, block, each part with its length). */
static void toy_hash(char domain, const uint8_t *const *parts,
		     const size_t *lens, int nparts, uint8_t *out, size_t ol)
{
	for (size_t blk = 0; blk * 8 < ol; blk++) {
		struct fnv f = { 0xcbf29ce484222325ULL };
		uint8_t d = (uint8_t)domain;

		fnv_add(&f, &d, 1);
		fnv_len(&f, blk);
		for (int i = 0; i < nparts; i++) {
			fnv_len(&f, lens[i]);
			fnv_add(&f, parts[i], lens[i]);
		}
		for (size_t i = 0; i < 8 && blk * 8 + i < ol; i++) {
			out[blk * 8 + i] = (uint8_t)(f.h >> (8 * i));
		}
	}
}

int wl_hmac_sha256(const uint8_t *key, size_t klen, const uint8_t *msg,
		   size_t mlen, uint8_t out[32])
{
	const uint8_t *p[] = { key, msg };
	const size_t l[] = { klen, mlen };

	if (failing()) {
		return -ENOMEM;
	}
	toy_hash('h', p, l, 2, out, 32);
	return 0;
}

int wl_hkdf_sha256(const uint8_t *salt, size_t sl, const uint8_t *ikm,
		   size_t il, const uint8_t *info, size_t inl, uint8_t *out,
		   size_t ol)
{
	const uint8_t *p[] = { salt, ikm, info };
	const size_t l[] = { sl, il, inl };

	if (failing()) {
		return -ENOMEM;
	}
	toy_hash('k', p, l, 3, out, ol);
	return 0;
}

/* Keystream: H(key, nonce) per 8-byte block; tag: H(key, nonce, aad, ct). */
static void toy_xor(const uint8_t key[32], const uint8_t nonce[12],
		    const uint8_t *in, size_t n, uint8_t *out)
{
	uint8_t ks[8];

	for (size_t off = 0; off < n; off += 8) {
		uint8_t blk[8];
		const uint8_t *p[] = { key, nonce, blk };
		const size_t l[] = { 32, 12, 8 };

		for (int i = 0; i < 8; i++) {
			blk[i] = (uint8_t)(off >> (8 * i));
		}
		toy_hash('s', p, l, 3, ks, 8);
		for (size_t i = 0; i < 8 && off + i < n; i++) {
			out[off + i] = in[off + i] ^ ks[i];
		}
	}
}

static void toy_tag(const uint8_t key[32], const uint8_t nonce[12],
		    const uint8_t *aad, size_t al, const uint8_t *ct, size_t n,
		    uint8_t tag[16])
{
	const uint8_t *p[] = { key, nonce, aad, ct };
	const size_t l[] = { 32, 12, al, n };

	toy_hash('t', p, l, 4, tag, 16);
}

int wl_aead_seal(const uint8_t key[32], const uint8_t nonce[12],
		 const uint8_t *aad, size_t al, const uint8_t *pt, size_t n,
		 uint8_t *ct, uint8_t tag[16])
{
	if (failing()) {
		return -ENOMEM;
	}
	toy_xor(key, nonce, pt, n, ct);
	toy_tag(key, nonce, aad, al, ct, n, tag);
	return 0;
}

int wl_aead_open(const uint8_t key[32], const uint8_t nonce[12],
		 const uint8_t *aad, size_t al, const uint8_t *ct, size_t n,
		 const uint8_t tag[16], uint8_t *pt)
{
	uint8_t want[16];

	if (failing()) {
		return -1;
	}
	toy_tag(key, nonce, aad, al, ct, n, want);
	if (!wl_ct_equal(want, tag, 16)) {
		return -1;	/* like the real one: nothing written */
	}
	toy_xor(key, nonce, ct, n, pt);
	return 0;
}

/* Deterministic and different on every call, so "a fresh nonce per M1" is
 * testable; reset by nothing, which is how a real RNG behaves too. */
int wl_random(uint8_t *out, size_t n)
{
	static uint64_t ctr;

	if (failing()) {
		return -ENOMEM;
	}
	for (size_t i = 0; i < n; i++) {
		ctr = ctr * 6364136223846793005ULL + 1442695040888963407ULL;
		out[i] = (uint8_t)(ctr >> 56);
	}
	return 0;
}

int wl_ct_equal(const uint8_t *a, const uint8_t *b, size_t n)
{
	uint8_t d = 0;

	for (size_t i = 0; i < n; i++) {
		d |= a[i] ^ b[i];
	}
	return d == 0;
}

int wl_crypto_selftest(void)
{
	return failing() ? -1 : 0;
}

/* The streaming hash: the same toy FNV, fed incrementally, so any chunking of
 * one stream gives one digest. Not SHA-256; the ota_net tests compute their
 * expected digest through these same three calls. */
static struct fnv img_fnv;
static size_t img_len;

int wl_sha256_start(void)
{
	if (failing()) {
		return -ENOMEM;
	}
	img_fnv.h = 0xcbf29ce484222325ULL;
	img_len = 0;
	return 0;
}

int wl_sha256_update(const uint8_t *p, size_t n)
{
	if (failing()) {
		return -ENOMEM;
	}
	fnv_add(&img_fnv, p, n);
	img_len += n;
	return 0;
}

int wl_sha256_finish(uint8_t out[32])
{
	if (failing()) {
		return -ENOMEM;
	}
	for (size_t blk = 0; blk < 4; blk++) {
		struct fnv f = img_fnv;
		uint8_t b = (uint8_t)blk;

		fnv_add(&f, &b, 1);
		fnv_len(&f, img_len);
		for (int i = 0; i < 8; i++) {
			out[blk * 8 + i] = (uint8_t)(f.h >> (8 * i));
		}
	}
	return 0;
}
