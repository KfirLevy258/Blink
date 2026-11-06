/* logo_write.c unchanged, on the RAM partition of tests/support/
 * flash_area_fake.c (spec S15.2). Run by tests/ci/check_host_tests.sh.
 *
 * What "present" may mean is the point: a stream that is cut, out of order,
 * over the bound or fails logo_parse leaves the partition erased, never half
 * a logo. */
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include "logo_write.h"
#include "logo_parse.h"

extern uint8_t fake_logo_part[];
extern int fake_logo_erases, fake_logo_fail_write, fake_logo_maps;
void fake_logo_reset(void);

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL %s\n", m); failures++; } \
	else { printf("PASS %s\n", m); } } while (0)

static uint8_t img[4096];
static size_t img_len;

/* One 320x240 frame, one colour: 596 runs of 129 and one of 116. */
static void build(void)
{
	uint8_t *b = img + LOGO_HDR_LEN;
	size_t n = 0;
	uint32_t px = 320 * 240, plen = 0;

	memset(img, 0, sizeof(img));
	memcpy(b, "BAN1\x40\x01\xf0\x00\x0f\x01\x01\x00", 12);
	n = 12;
	b[n++] = 1;
	memset(b + n, 0, 8);
	b[n + 4] = 0x40; b[n + 5] = 0x01; b[n + 6] = 0xf0;
	n += 12;
	while (px > 0) {
		uint32_t k = px > 129 ? 129 : px;

		b[n++] = (uint8_t)(k + 126);
		b[n++] = 0xf8;
		b[n++] = 0x00;
		plen += 3;
		px -= k;
	}
	b[13 + 8] = plen & 0xff;
	b[13 + 9] = plen >> 8;
	memcpy(img, "BLGO\x01\x00\xb8\x0b", 8);	/* version 1, hold 3000 */
	uint32_t c = logo_crc32(b, n);

	for (int i = 0; i < 4; i++) {
		img[8 + i] = (uint8_t)(n >> (8 * i));
		img[12 + i] = (uint8_t)(c >> (8 * i));
	}
	img_len = LOGO_HDR_LEN + n;
}

static bool erased(void)
{
	for (size_t i = 0; i < LOGO_IMAGE_MAX; i++) {
		if (fake_logo_part[i] != 0xff) {
			return false;
		}
	}
	return true;
}

/* begin, then the image in 256-byte chunks; the result of the last call. */
static int stream(const uint8_t *p, size_t n, uint32_t crc)
{
	int rc = logo_write_begin((uint32_t)n, crc);

	for (size_t off = 0; rc >= 0 && off < n; off += 256) {
		size_t k = n - off < 256 ? n - off : 256;

		rc = logo_write_chunk((uint32_t)off, p + off, k);
	}
	return rc;
}

int main(void)
{
	uint32_t bytes = 0;
	uint16_t frames = 0;

	build();
	uint32_t crc = logo_crc32(img, img_len);

	/* in order, then end: present, described, and the map let go */
	fake_logo_reset();
	memset(fake_logo_part, 0x5a, 64);	/* a previous logo's bytes */
	CHECK(logo_write_begin((uint32_t)img_len, crc) == 0 && erased() &&
	      fake_logo_erases == 1, "begin_erases_the_partition");
	CHECK(logo_write_chunk(0, img, 256) == 256 &&
	      logo_write_chunk(256, img + 256, 256) == 512,
	      "in_order_chunks_return_the_count");
	fake_logo_reset();
	CHECK(stream(img, img_len, crc) == (int)img_len, "whole_stream_accepted");
	CHECK(memcmp(fake_logo_part, img, img_len) == 0, "bytes_land_in_place");
	CHECK(logo_write_end() == 0, "end_parses_and_reports_present");
	CHECK(logo_write_state(&bytes, &frames) && bytes == img_len &&
	      frames == 1, "state_says_present_with_bytes_and_frames");
	CHECK(fake_logo_maps == 0, "end_unmaps");
	CHECK(!logo_write_active(), "end_closes_the_stream");

	/* gap and rewind */
	fake_logo_reset();
	logo_write_begin((uint32_t)img_len, crc);
	logo_write_chunk(0, img, 256);
	CHECK(logo_write_chunk(512, img + 512, 256) == -1 && erased() &&
	      !logo_write_active(), "gap_refused_and_erased");
	fake_logo_reset();
	logo_write_begin((uint32_t)img_len, crc);
	logo_write_chunk(0, img, 256);
	logo_write_chunk(256, img + 256, 256);
	CHECK(logo_write_chunk(256, img + 256, 256) == -1 && erased(),
	      "rewind_refused_and_erased");
	CHECK(logo_write_chunk(512, img + 512, 256) == -1,
	      "nothing_accepted_after_a_refusal");

	/* the bound */
	fake_logo_reset();
	CHECK(logo_write_begin(LOGO_IMAGE_MAX + 1u, 0) == -EINVAL &&
	      fake_logo_erases == 0, "begin_over_the_bound_refused_unerased");
	CHECK(logo_write_begin(0, 0) == -EINVAL, "begin_of_nothing_refused");
	CHECK(logo_write_begin(LOGO_IMAGE_MAX, 0) == 0, "begin_at_the_bound");
	{
		static uint8_t big[512];
		int rc = 0;

		for (uint32_t off = 0; rc >= 0 && off < LOGO_IMAGE_MAX;
		     off += sizeof(big)) {
			rc = logo_write_chunk(off, big, sizeof(big));
		}
		CHECK(rc == (int)LOGO_IMAGE_MAX, "writes_up_to_the_bound");
		CHECK(logo_write_chunk(LOGO_IMAGE_MAX, big, 1) == -1,
		      "a_byte_past_the_bound_refused");
	}
	fake_logo_reset();
	logo_write_begin(300, 0);
	CHECK(logo_write_chunk(0, img, 301) == -1, "a_chunk_past_size_refused");

	/* end: logo_parse runs, and a failure erases */
	fake_logo_reset();
	{
		static uint8_t bad[4096];

		memcpy(bad, img, img_len);
		bad[LOGO_HDR_LEN + 40] ^= 1;	/* the CRC in the header no longer matches */
		stream(bad, img_len, logo_crc32(bad, img_len));
		CHECK(logo_write_end() != 0 && erased(),
		      "end_runs_logo_parse_and_erases_on_failure");
		CHECK(!logo_write_state(&bytes, &frames), "state_absent_after_a_rejection");
	}
	fake_logo_reset();
	stream(img, img_len, crc ^ 1);
	CHECK(logo_write_end() != 0 && erased(), "end_refuses_the_wrong_stream_crc");
	fake_logo_reset();
	logo_write_begin((uint32_t)img_len, crc);
	logo_write_chunk(0, img, 256);
	CHECK(logo_write_end() != 0 && erased(), "end_before_the_last_byte_refused");
	CHECK(logo_write_end() != 0, "end_with_nothing_open_refused");

	/* a flash error */
	fake_logo_reset();
	logo_write_begin((uint32_t)img_len, crc);
	fake_logo_fail_write = 1;
	CHECK(logo_write_chunk(0, img, 256) == -1 && !logo_write_active(),
	      "a_flash_error_drops_the_stream");

	/* remove, abort */
	fake_logo_reset();
	stream(img, img_len, crc);
	logo_write_end();
	CHECK(logo_write_remove() == 0 && erased() &&
	      !logo_write_state(&bytes, &frames), "remove_erases");
	fake_logo_reset();
	logo_write_begin((uint32_t)img_len, crc);
	logo_write_chunk(0, img, 256);
	logo_write_abort();
	CHECK(erased() && !logo_write_active(), "abort_erases");
	fake_logo_reset();
	stream(img, img_len, crc);
	logo_write_end();
	fake_logo_erases = 0;
	logo_write_abort();
	CHECK(fake_logo_erases == 0 && logo_write_state(&bytes, &frames),
	      "abort_with_nothing_open_keeps_the_logo");

	printf("%d failure(s)\n", failures);
	return failures ? 1 : 0;
}
