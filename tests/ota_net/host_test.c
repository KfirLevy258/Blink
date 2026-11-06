/* ota_net.c on a laptop: the image over the session, against a RAM slot 1
 * (tests/support/flash_img_fake.c) and a toy hash (wl_crypto_fake.c). */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ota_net.h"
#include "flash_img_port.h"
#include "wl_crypto.h"

extern uint8_t fake_slot1[];
extern int fake_erases, fake_upgrade_requests, fake_upgrade_arg;
extern bool fake_flash_inited;
void fake_flash_reset(void);

static int failures;
#define CK(cond, name) do { if (cond) { printf("PASS: %s\n", name); } \
	else { printf("FAIL: %s\n", name); failures++; } } while (0)

static bool unconfirmed;
bool ota_unconfirmed(void) { return unconfirmed; }

static uint8_t *img;

/* A valid MCUboot header for 99.0.0 (running is 1.4.x), then a body that
 * differs byte to byte, so a misplaced chunk is visible. */
static void make_image(size_t n)
{
	free(img);
	img = malloc(n);
	for (size_t i = 0; i < n; i++) {
		img[i] = (uint8_t)(i * 31 + (i >> 8));
	}
	memset(img, 0, 28);
	img[0] = 0x3d; img[1] = 0xb8; img[2] = 0xf3; img[3] = 0x96;
	img[20] = 99;
}

static void manifest(struct ota_manifest *m, size_t n)
{
	uint8_t d[32];

	memset(m, 0, sizeof(*m));
	strcpy(m->version, "99.0.0");
	m->size = (uint32_t)n;
	wl_sha256_start();
	wl_sha256_update(img, n);
	wl_sha256_finish(d);
	for (int i = 0; i < 32; i++) {
		snprintf(&m->sha256[i * 2], 3, "%02x", d[i]);
	}
}

/* Stream [from, to) in 512-byte chunks; the last returns what chunk returned. */
static int feed(size_t from, size_t to)
{
	int r = 0;

	for (size_t o = from; o < to && r >= 0; o += 512) {
		size_t k = to - o < 512 ? to - o : 512;

		r = ota_net_chunk((uint32_t)o, img + o, k);
	}
	return r;
}

static void fresh(void)
{
	fake_flash_reset();
	fake_flash_inited = false;
	unconfirmed = false;
	ota_net_abort();
}

int main(void)
{
	struct ota_manifest m;
	size_t n = 1000003;		/* not a multiple of 512 or 16384 */

	make_image(n);
	manifest(&m, n);

	fresh();
	CK(ota_net_begin(&m) == 0 && ota_net_active(), "begin_opens");
	CK(feed(0, n) == (int)n, "stream_accepts_every_byte");
	CK(ota_net_end(&m) == OTA_OK, "end_seals");
	CK(fake_upgrade_requests == 1 && fake_upgrade_arg == BOOT_UPGRADE_TEST,
	   "whole_stream_of_odd_size_seals");
	CK(memcmp(fake_slot1, img, n) == 0 && fake_slot1[n] == 0xff,
	   "tail_flushed_and_nothing_after_it");
	CK(!ota_net_active(), "sealed_stream_is_closed");

	fresh();
	{
		/* Acks the daemon will see: 61 boundaries plus the end. */
		int acks = 0, got = 0;

		ota_net_begin(&m);
		for (size_t o = 0; o < n; o += 512) {
			size_t k = n - o < 512 ? n - o : 512;
			int r = ota_net_chunk((uint32_t)o, img + o, k);

			acks += ota_net_should_ack((uint32_t)got, (uint32_t)r, m.size);
			got = r;
		}
		CK(acks == (int)(n / OTA_ACK_EVERY) + 1, "acks_at_each_boundary_and_the_end");
	}

	/* Bounds: a manifest of exactly OTA_IMAGE_MAX is the largest accepted. */
	fresh();
	{
		size_t big = OTA_IMAGE_MAX;
		struct ota_manifest bm;

		make_image(big + 4096);
		manifest(&bm, big);
		CK(ota_net_begin(&bm) == 0, "image_max_itself_is_accepted");
		CK(feed(0, big - 512) == (int)(big - 512), "up_to_the_last_block");
		CK(ota_net_chunk((uint32_t)(big - 512), img + big - 512, 1024) == -1,
		   "chunk_past_image_max_refused");
		CK(!ota_net_active() && fake_upgrade_requests == 0, "oversized_requests_nothing");
		int clean = 1;

		for (size_t i = OTA_IMAGE_MAX; i < 1376256; i++) {
			clean &= fake_slot1[i] == 0xff;
		}
		CK(clean, "oversized_chunk_touches_no_trailer");
		CK(ota_net_chunk((uint32_t)(big - 512), img + big - 512, 512) == -1,
		   "a_following_chunk_is_refused");
		make_image(n);
		manifest(&m, n);
	}

	fresh();
	{
		struct ota_manifest bm = m;

		bm.size = OTA_IMAGE_MAX + 1;
		CK(ota_net_begin(&bm) == -EINVAL && !ota_net_active() && !fake_flash_inited,
		   "manifest_over_image_max_refused_at_begin");
		bm.size = 0;
		CK(ota_net_begin(&bm) == -EINVAL, "empty_manifest_refused_at_begin");
	}

	fresh();
	unconfirmed = true;
	CK(ota_net_begin(&m) == -EBUSY && !ota_net_active() && !fake_flash_inited &&
	   fake_erases == 0, "begin_refused_while_unconfirmed");

	fresh();
	ota_net_begin(&m);
	feed(0, 40960);
	CK(fake_erases > 0, "chunks_reach_flash");
	ota_net_abort();
	CK(!ota_net_active() && fake_upgrade_requests == 0, "abort_requests_nothing");
	CK(ota_net_end(&m) != OTA_OK && fake_upgrade_requests == 0, "aborted_stream_cannot_end");

	fresh();
	ota_net_begin(&m);
	feed(0, 40960);
	ota_net_service(true, true, false);
	CK(!ota_net_active() && fake_upgrade_requests == 0 && fake_erases > 0 &&
	   ota_net_chunk(40960, img + 40960, 512) == -1, "usb_takeover_aborts_stream");

	fresh();
	ota_net_begin(&m);
	feed(0, 40960);
	ota_net_service(false, false, false);
	CK(!ota_net_active() && fake_upgrade_requests == 0 &&
	   ota_net_chunk(40960, img + 40960, 512) == -1, "session_drop_aborts_stream");

	fresh();
	ota_net_begin(&m);
	feed(0, 40960);
	ota_net_service(false, true, true);
	CK(!ota_net_active() && fake_upgrade_requests == 0 &&
	   ota_net_chunk(40960, img + 40960, 512) == -1, "replacement_aborts_stream");

	fresh();
	ota_net_begin(&m);
	feed(0, 2048);
	ota_net_service(false, true, false);
	CK(ota_net_active() && ota_net_chunk(2048, img + 2048, 512) == 2560,
	   "a_healthy_session_is_left_alone");
	fresh();
	{
		int e = fake_erases;

		ota_net_service(true, false, true);
		CK(!ota_net_active() && fake_erases == e, "service_with_no_stream_does_nothing");
	}

	/* partial_stream_not_sealed: ota_end before the last byte. */
	fresh();
	ota_net_begin(&m);
	feed(0, n - 512);
	CK(ota_net_end(&m) == OTA_ERR_SIZE && fake_upgrade_requests == 0 && !ota_net_active(),
	   "partial_stream_not_sealed");

	/* gap and rewind are refused and drop the stream. */
	fresh();
	ota_net_begin(&m);
	feed(0, 1024);
	CK(ota_net_chunk(1536, img + 1536, 512) == -1 && !ota_net_active(), "gap_refused");
	fresh();
	ota_net_begin(&m);
	feed(0, 1024);
	CK(ota_net_chunk(512, img + 512, 512) == -1 && !ota_net_active(), "rewind_refused");

	/* The first chunk's header: not newer, not offered, not an image. */
	fresh();
	ota_net_begin(&m);
	img[20] = 1; img[21] = 4; img[22] = 0;
	CK(ota_net_chunk(0, img, 512) == -1 && !ota_net_active(), "same_version_refused_on_first_chunk");
	img[20] = 98;
	fresh();
	ota_net_begin(&m);
	CK(ota_net_chunk(0, img, 512) == -1, "newer_but_not_offered_refused_on_first_chunk");
	img[20] = 99; img[21] = 0;
	fresh();
	ota_net_begin(&m);
	CK(ota_net_chunk(0, img, 20) == -1, "first_chunk_shorter_than_a_header_refused");

	/* A flipped byte: right length, wrong hash, nothing requested. */
	fresh();
	ota_net_begin(&m);
	img[n / 2] ^= 1;
	feed(0, n);
	img[n / 2] ^= 1;
	CK(ota_net_end(&m) == OTA_ERR_HASH && fake_upgrade_requests == 0, "hash_mismatch_not_sealed");

	/* A chunk past the manifest's size, though inside the image. */
	fresh();
	ota_net_begin(&m);
	feed(0, n - 512);
	CK(ota_net_chunk((uint32_t)(n - 512), img + n - 512, 1024) == -1, "chunk_past_manifest_size_refused");

	/* A flash error drops the stream. */
	fresh();
	ota_net_begin(&m);
	{
		extern int fake_fail_write;

		fake_fail_write = 1;
		CK(ota_net_chunk(0, img, 512) == -1 && !ota_net_active(), "flash_error_drops_stream");
	}

	/* A second begin starts again from zero. */
	fresh();
	ota_net_begin(&m);
	feed(0, 4096);
	ota_net_begin(&m);
	CK(ota_net_chunk(4096, img + 4096, 512) == -1, "new_begin_starts_from_zero");
	ota_net_begin(&m);
	CK(ota_net_chunk(0, img, 512) == 512, "and_the_first_chunk_is_accepted");

	printf(failures ? "\n%d FAILED\n" : "\nALL PASSED\n", failures);
	return failures ? 1 : 0;
}
