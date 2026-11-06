/* firmware/src/wl_frame.c, unchanged, on the toy cipher of
 * tests/support/wl_crypto_fake.c: counters, lengths checked before a body is
 * read, the dead-stays-dead rule, and every refusal. That the real cipher
 * and the wire layout give the PC's bytes is tests/wl_wire_real.
 */
#include <stdio.h>
#include <string.h>

#include "wl_crypto.h"
#include "wl_frame.h"

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

static void pair(struct wl_dir *tx, struct wl_dir *rx)
{
	memset(tx, 0, sizeof(*tx));
	memset(tx->key, 0x33, 32);
	*rx = *tx;
}

static uint8_t frame[WL_FRAME_MAX], body[WL_PT_MAX];

static void test_round_trip(void)
{
	struct wl_dir tx, rx;
	uint8_t type = 0;
	int fl, bl;

	pair(&tx, &rx);
	fl = wl_frame_seal(&tx, WL_T_LINE, (const uint8_t *)"hello", 5, frame,
			   sizeof(frame));
	check(fl == 2 + 6 + 16 && frame[0] == 0 && frame[1] == 6 &&
		      wl_frame_len(frame) == 6,
	      "seal_layout");
	bl = wl_frame_open(&rx, frame, (size_t)fl, &type, body, sizeof(body));
	check(bl == 5 && type == 'L' && memcmp(body, "hello", 5) == 0,
	      "seal_open_round_trip");
	check(tx.ctr == 1 && rx.ctr == 1, "counter_advances");
	check(wl_frame_open(&rx, frame, (size_t)fl, &type, body,
			    sizeof(body)) == -1 &&
		      rx.dead && rx.ctr == 1,
	      "replayed_frame_refused");

	pair(&tx, &rx);
	{
		static uint8_t f2[WL_FRAME_MAX];
		int l1 = wl_frame_seal(&tx, 'L', (const uint8_t *)"a", 1, frame,
				       sizeof(frame));
		int l2 = wl_frame_seal(&tx, 'L', (const uint8_t *)"b", 1, f2,
				       sizeof(f2));

		check(l1 > 0 && l2 > 0 &&
			      wl_frame_open(&rx, f2, (size_t)l2, &type, body,
					    sizeof(body)) == -1 &&
			      rx.dead,
		      "reordered_frame_refused");
	}

	pair(&tx, &rx);
	fl = wl_frame_seal(&tx, WL_T_LOGO, (const uint8_t *)"\0\0\0\1x", 5,
			   frame, sizeof(frame));
	bl = wl_frame_open(&rx, frame, (size_t)fl, &type, body, sizeof(body));
	check(bl == 5 && type == 'G' && body[3] == 1 && body[4] == 'x',
	      "type_G_round_trips");
	fl = wl_frame_seal(&tx, WL_T_OTA, (const uint8_t *)"z", 1, frame,
			   sizeof(frame));
	check(wl_frame_open(&rx, frame, (size_t)fl, &type, body,
			    sizeof(body)) == 1 && type == 'O',
	      "type_O_round_trips");
	fl = wl_frame_seal(&tx, 'X', (const uint8_t *)"z", 1, frame,
			   sizeof(frame));
	type = 0;
	check(fl > 0 && wl_frame_open(&rx, frame, (size_t)fl, &type, body,
				      sizeof(body)) == -1 &&
		      rx.dead && type == 0,
	      "unknown_type_refused");
}

static void test_refusals(void)
{
	struct wl_dir tx, rx;
	uint8_t type = 0, hdr[2];
	int fl;

	pair(&tx, &rx);
	fl = wl_frame_seal(&tx, 'L', (const uint8_t *)"hello", 5, frame,
			   sizeof(frame));
	frame[1] ^= 1;
	memset(body, 0x99, sizeof(body));
	check(wl_frame_open(&rx, frame, (size_t)fl, &type, body,
			    sizeof(body)) == -1 && rx.dead && body[0] == 0x99,
	      "tampered_len_refused");

	pair(&tx, &rx);
	fl = wl_frame_seal(&tx, 'L', (const uint8_t *)"hello", 5, frame,
			   sizeof(frame));
	frame[fl - 1] ^= 1;
	check(wl_frame_open(&rx, frame, (size_t)fl, &type, body,
			    sizeof(body)) == -1 && rx.dead && body[0] == 0x99,
	      "tampered_tag_refused");

	pair(&tx, &rx);
	fl = wl_frame_seal(&tx, 'L', (const uint8_t *)"hello", 5, frame,
			   sizeof(frame));
	frame[3] ^= 1;
	check(wl_frame_open(&rx, frame, (size_t)fl, &type, body,
			    sizeof(body)) == -1 && rx.dead,
	      "tampered_body_refused");

	hdr[0] = 0;
	hdr[1] = 0;
	check(wl_frame_len(hdr) == -1, "len_zero_refused");
	hdr[1] = 1;
	check(wl_frame_len(hdr) == 1, "len_one_accepted");
	hdr[0] = 0x02;
	hdr[1] = 0x05;
	check(wl_frame_len(hdr) == 517, "len_517_accepted");
	hdr[1] = 0x06;
	check(wl_frame_len(hdr) == -1, "len_518_refused_before_body");
	hdr[0] = hdr[1] = 0xff;
	check(wl_frame_len(hdr) == -1, "len_65535_refused_before_body");

	/* A header that fits wl_frame_len but whose frame is cut short. */
	pair(&tx, &rx);
	fl = wl_frame_seal(&tx, 'L', (const uint8_t *)"hello", 5, frame,
			   sizeof(frame));
	check(wl_frame_open(&rx, frame, (size_t)fl - 1, &type, body,
			    sizeof(body)) == -1 && rx.dead,
	      "short_frame_refused");
	pair(&tx, &rx);
	check(wl_frame_open(&rx, frame, 1, &type, body, sizeof(body)) == -1 &&
		      rx.dead,
	      "shorter_than_header_refused");

	/* A caller buffer that cannot hold the body. */
	pair(&tx, &rx);
	fl = wl_frame_seal(&tx, 'L', (const uint8_t *)"hello", 5, frame,
			   sizeof(frame));
	check(wl_frame_open(&rx, frame, (size_t)fl, &type, body, 4) == -1 &&
		      rx.dead,
	      "body_buffer_too_small_refused");
}

static void test_dead(void)
{
	struct wl_dir tx, rx;
	uint8_t type;
	int fl;

	pair(&tx, &rx);
	fl = wl_frame_seal(&tx, 'L', (const uint8_t *)"a", 1, frame,
			   sizeof(frame));
	frame[fl - 1] ^= 1;
	wl_frame_open(&rx, frame, (size_t)fl, &type, body, sizeof(body));
	/* Even the genuine next frame is refused: a direction in doubt is
	 * closed, never resynchronised. */
	pair(&tx, &(struct wl_dir){ 0 });
	fl = wl_frame_seal(&tx, 'L', (const uint8_t *)"a", 1, frame,
			   sizeof(frame));
	check(rx.dead && wl_frame_open(&rx, frame, (size_t)fl, &type, body,
				       sizeof(body)) == -1,
	      "dead_stays_dead");
	check(wl_frame_seal(&rx, 'L', (const uint8_t *)"a", 1, frame,
			    sizeof(frame)) == -1,
	      "dead_cannot_seal");

	pair(&tx, &rx);
	wl_fake_fail_next = 1;
	memset(frame, 0x66, sizeof(frame));
	check(wl_frame_seal(&tx, 'L', (const uint8_t *)"a", 1, frame,
			    sizeof(frame)) == -1 && tx.dead && tx.ctr == 0 &&
		      frame[0] == 0 && frame[40] == 0,
	      "seal_failure_sets_dead");
	check(wl_frame_seal(&tx, 'L', (const uint8_t *)"a", 1, frame,
			    sizeof(frame)) == -1,
	      "seal_after_failure_stays_dead");

	pair(&tx, &rx);
	check(wl_frame_seal(&tx, 'L', body, WL_PT_MAX, frame,
			    sizeof(frame)) == -1 && tx.dead,
	      "body_over_max_refused_on_seal");
	pair(&tx, &rx);
	check(wl_frame_seal(&tx, 'L', body, WL_PT_MAX - 1, frame,
			    sizeof(frame)) == WL_FRAME_MAX,
	      "max_body_fits");
	pair(&tx, &rx);
	check(wl_frame_seal(&tx, 'L', body, 5, frame, 2 + 6 + 15) == -1 &&
		      tx.dead,
	      "out_buffer_too_small_refused");

	/* A 512-byte line, its newline included (protocol.MAX_LINE_BYTES). */
	pair(&tx, &rx);
	memset(body, 'x', 512);
	fl = wl_frame_seal(&tx, 'L', body, 512, frame, sizeof(frame));
	{
		static uint8_t back[WL_PT_MAX];

		check(fl == 2 + 513 + 16 &&
			      wl_frame_open(&rx, frame, (size_t)fl, &type, back,
					    sizeof(back)) == 512 &&
			      memcmp(back, body, 512) == 0,
		      "line_of_512_fits_a_frame");
	}

	pair(&tx, &rx);
	tx.ctr = rx.ctr = UINT64_MAX;
	check(wl_frame_seal(&tx, 'L', (const uint8_t *)"a", 1, frame,
			    sizeof(frame)) == -1 && tx.dead,
	      "counter_at_max_refused_on_seal");
	pair(&tx, &rx);
	fl = wl_frame_seal(&tx, 'L', (const uint8_t *)"a", 1, frame,
			   sizeof(frame));
	rx.ctr = UINT64_MAX;
	check(wl_frame_open(&rx, frame, (size_t)fl, &type, body,
			    sizeof(body)) == -1 && rx.dead,
	      "counter_at_max_refused_on_open");

	pair(&tx, &rx);
	tx.ctr = rx.ctr = UINT64_MAX - 1;
	fl = wl_frame_seal(&tx, 'L', (const uint8_t *)"a", 1, frame,
			   sizeof(frame));
	check(fl > 0 && wl_frame_open(&rx, frame, (size_t)fl, &type, body,
				      sizeof(body)) == 1 &&
		      tx.ctr == UINT64_MAX,
	      "last_counter_still_usable");

	wl_dir_wipe(&tx);
	{
		int z = 1;

		for (size_t i = 0; i < sizeof(tx); i++) {
			z &= ((uint8_t *)&tx)[i] == 0;
		}
		check(z, "dir_wipe_zeroes");
	}
}

int main(void)
{
	test_round_trip();
	test_refusals();
	test_dead();
	return fails ? 1 : 0;
}
