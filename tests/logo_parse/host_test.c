/* The strict logo_parse (spec S15.2, D60): every limit broken one at a time,
 * on blobs built here, never captured. Run by tests/ci/check_host_tests.sh.
 *
 * The first case is the reason this exists: 65535 empty frames at 1 fps
 * passed the old CRC-and-header check, and ui_boot.c would have held the boot
 * on it for 18 hours. */
#include <stdio.h>
#include <string.h>
#include "logo_parse.h"

#define W 320
#define H 240

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL %s\n", m); failures++; } \
	else { printf("PASS %s\n", m); } } while (0)

static uint8_t part[0x80000];
static uint8_t blob[0x80000 - LOGO_HDR_LEN];
static size_t bn;

static void p8(unsigned v) { blob[bn++] = (uint8_t)v; }
static void p16(unsigned v) { p8(v & 0xff); p8(v >> 8); }
static void p32(uint32_t v) { p16(v & 0xffff); p16(v >> 16); }

static void ban1(unsigned fps, unsigned nframes)
{
	bn = 0;
	memcpy(blob, "BAN1", 4);
	bn = 4;
	p16(W);
	p16(H);
	p8(fps);
	p8(1);		/* flags: big-endian pixels, as the encoders write */
	p16(nframes);
}

/* A rect header and its PackBits16 payload: `px` pixels as runs of at most
 * 129 (control byte n + 126, then the pixel), a lone pixel as a literal. */
static void rect_px(unsigned x, unsigned y, unsigned w, unsigned h, uint32_t px)
{
	size_t at = bn;
	uint32_t plen = 0;

	p16(x); p16(y); p16(w); p16(h); p32(0);
	while (px > 0) {
		uint32_t n = px > 129 ? 129 : px;

		if (n == 1) {		/* a literal of one */
			p8(0); p8(0x12); p8(0x34);
		} else {
			p8(n + 126); p8(0x12); p8(0x34);
		}
		plen += 3;
		px -= n;
	}
	blob[at + 8] = plen & 0xff;
	blob[at + 9] = (plen >> 8) & 0xff;
	blob[at + 10] = (plen >> 16) & 0xff;
	blob[at + 11] = plen >> 24;
}

static void rect(unsigned x, unsigned y, unsigned w, unsigned h)
{
	rect_px(x, y, w, h, (uint32_t)w * h);
}

/* A rect with a hand-written payload. */
static void rect_raw(unsigned w, unsigned h, uint32_t plen,
		     const uint8_t *pay, size_t paylen)
{
	p16(0); p16(0); p16(w); p16(h); p32(plen);
	memcpy(blob + bn, pay, paylen);
	bn += paylen;
}

static size_t seal(unsigned hold_ms)
{
	memset(part, 0xff, sizeof(part));
	memset(part, 0, LOGO_HDR_LEN);
	memcpy(part, "BLGO", 4);
	part[4] = LOGO_VERSION;
	part[6] = hold_ms & 0xff;
	part[7] = hold_ms >> 8;
	uint32_t c = logo_crc32(blob, bn);

	for (int i = 0; i < 4; i++) {
		part[8 + i] = (uint8_t)(bn >> (8 * i));
		part[12 + i] = (uint8_t)(c >> (8 * i));
	}
	memcpy(part + LOGO_HDR_LEN, blob, bn);
	return sizeof(part);
}

static bool ok(unsigned hold_ms)
{
	struct logo_info info;

	return logo_parse(part, seal(hold_ms), W, H, &info);
}

/* frame 0 full, then n - 1 frames of one small rect each */
static void clip(unsigned fps, unsigned n)
{
	ban1(fps, n);
	p8(1);
	rect(0, 0, W, H);
	for (unsigned i = 1; i < n; i++) {
		p8(1);
		rect(10, 10, 8, 8);
	}
}

int main(void)
{
	struct logo_info info;

	clip(10, 1);
	CHECK(ok(3000), "good_still");
	CHECK(logo_parse(part, sizeof(part), W, H, &info) &&
	      info.nframes == 1 && info.hold_ms == 3000, "good_still_described");

	clip(10, 12);
	CHECK(ok(500), "good_clip_12_at_10fps");

	ban1(1, 65535);
	for (unsigned i = 0; i < 65535; i++) {
		p8(0);
	}
	CHECK(!ok(0), "frames_65535_empty_at_1fps_refused");

	clip(60, LOGO_MAX_FRAMES);
	CHECK(ok(0), "frames_at_max_accepted");
	clip(60, LOGO_MAX_FRAMES + 1);
	CHECK(!ok(0), "frames_over_max_refused");

	clip(0, 1);
	CHECK(!ok(0), "fps_0_refused");
	clip(LOGO_FPS_MAX, 1);
	CHECK(ok(0), "fps_at_max_accepted");
	clip(LOGO_FPS_MAX + 1, 1);
	CHECK(!ok(0), "fps_over_max_refused");

	clip(1, LOGO_MAX_TOTAL_MS / 1000);
	CHECK(ok(0), "total_at_max_accepted");
	clip(1, LOGO_MAX_TOTAL_MS / 1000 + 1);
	CHECK(!ok(0), "total_over_max_refused");
	clip(1, 10);
	CHECK(ok(LOGO_MAX_TOTAL_MS - 10000), "total_at_max_with_hold_accepted");
	CHECK(!ok(LOGO_MAX_TOTAL_MS - 10000 + 1),
	      "total_over_max_with_hold_refused");

	ban1(10, 1);
	p8(LOGO_MAX_RECTS);
	for (unsigned i = 0; i < LOGO_MAX_RECTS; i++) {
		rect(i, i, 1, 1);
	}
	CHECK(ok(0), "rects_at_max_accepted");
	ban1(10, 1);
	p8(LOGO_MAX_RECTS + 1);
	for (unsigned i = 0; i < LOGO_MAX_RECTS + 1; i++) {
		rect(i, i, 1, 1);
	}
	CHECK(!ok(0), "rects_over_max_refused");

	ban1(10, 3);
	p8(1);
	rect(0, 0, W, H);
	p8(0);
	p8(0);
	CHECK(ok(0), "zero_rect_hold_frame_accepted");

	ban1(10, 1);
	p8(1);
	rect(W - 10, 0, 11, 1);
	CHECK(!ok(0), "rect_outside_canvas_refused");
	ban1(10, 1);
	p8(1);
	rect(0, H - 1, 1, 2);
	CHECK(!ok(0), "rect_outside_canvas_bottom_refused");

	ban1(10, 1);
	p8(1);
	rect(0, 0, 4, 0);
	CHECK(!ok(0), "rect_zero_size_refused");
	ban1(10, 1);
	p8(1);
	rect(0, 0, 0, 4);
	CHECK(!ok(0), "rect_zero_width_refused");

	{
		/* 2x2 = 4 pixels; a run of 4 is control 4 + 126 = 130. */
		static const uint8_t run4[] = { 130, 0x12, 0x34 };
		static const uint8_t run3[] = { 129, 0x12, 0x34 };
		static const uint8_t run5[] = { 131, 0x12, 0x34 };
		static const uint8_t trail[] = { 130, 0x12, 0x34, 0x00 };
		/* a literal of 4 pixels (control 3) with only 3 of them */
		static const uint8_t lit_short[] = { 3, 1, 2, 3, 4, 5, 6 };

		ban1(10, 1); p8(1); rect_raw(2, 2, 3, run4, 3);
		CHECK(ok(0), "payload_exact_accepted");

		/* the declared payload runs past the blob */
		ban1(10, 1); p8(1); rect_raw(2, 2, 4, run4, 3);
		CHECK(!ok(0), "payload_short_refused");

		ban1(10, 1); p8(1); rect_raw(2, 2, 3, run3, 3);
		CHECK(!ok(0), "payload_decodes_to_wrong_pixel_count_refused");

		ban1(10, 1); p8(1); rect_raw(2, 2, 3, run5, 3);
		CHECK(!ok(0), "run_overlong_refused");

		ban1(10, 1); p8(1); rect_raw(2, 2, 4, trail, 4);
		CHECK(!ok(0), "payload_trailing_bytes_refused");

		ban1(10, 1); p8(1); rect_raw(2, 2, 7, lit_short, 7);
		CHECK(!ok(0), "payload_short_run_refused");
	}

	/* three frames declared, two present */
	clip(10, 2);
	blob[10] = 3;
	CHECK(!ok(0), "walk_ends_past_blob_refused");

	clip(10, 2);
	p8(0);		/* a byte after the last frame, inside blob_len */
	CHECK(!ok(0), "walk_ends_short_of_blob_refused");

	clip(10, 1);
	blob[9] = 0;	/* flags: little-endian pixels, which no encoder writes */
	CHECK(!ok(0), "flags_other_than_big_endian_refused");

	printf("%d failure(s)\n", failures);
	return failures ? 1 : 0;
}
