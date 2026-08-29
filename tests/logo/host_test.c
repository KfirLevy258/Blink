/* Standalone host test for logo_parse (see tests/ci/check_host_tests.sh).
 * Build & run:
 *   cc -I ../../firmware/src host_test.c ../../firmware/src/logo_parse.c \
 *      ../../firmware/src/bootanim_dec.c -o /tmp/logotest && /tmp/logotest
 */
#include <stdio.h>
#include <string.h>
#include "logo_parse.h"

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

/* A one-frame 4x3 BAN1 blob: a single RLE run of 12 x 0xBEEF. */
static const uint8_t blob[] = {
	'B', 'A', 'N', '1', 4, 0, 3, 0, 12, 1, 1, 0,
	1,
	0, 0, 0, 0, 4, 0, 3, 0, 3, 0, 0, 0,
	0x8a, 0xbe, 0xef,
};

static void wr16(uint8_t *p, uint16_t v)
{
	p[0] = v & 0xff;
	p[1] = v >> 8;
}

static void wr32(uint8_t *p, uint32_t v)
{
	p[0] = v & 0xff;
	p[1] = (v >> 8) & 0xff;
	p[2] = (v >> 16) & 0xff;
	p[3] = v >> 24;
}

/* Build a partition image: a valid header, the blob, then erased flash. */
static void build(uint8_t *part, size_t part_len)
{
	memset(part, 0xff, part_len);
	memset(part, 0, LOGO_HDR_LEN);
	memcpy(part, "BLGO", 4);
	wr16(part + 4, LOGO_VERSION);
	wr16(part + 6, 2500);
	wr32(part + 8, sizeof(blob));
	wr32(part + 12, logo_crc32(blob, sizeof(blob)));
	part[16] = 0x07;
	part[17] = 0x0b;
	part[18] = 0x1e;
	memcpy(part + LOGO_HDR_LEN, blob, sizeof(blob));
}

int main(void)
{
	uint8_t part[256];
	struct logo_info info;

	/* The CRC must be the zlib one, or the Python tool and this parser
	 * disagree about every logo ever built. "123456789" -> 0xcbf43926 is
	 * the published check value for CRC-32/ISO-HDLC. */
	CHECK(logo_crc32((const uint8_t *)"123456789", 9) == 0xcbf43926u,
	      "crc32 matches the IEEE/zlib check value");
	CHECK(logo_crc32(NULL, 0) == 0, "crc32 of nothing is 0");

	build(part, sizeof(part));
	CHECK(logo_parse(part, sizeof(part), 4, 3, &info),
	      "a valid image parses");
	CHECK(info.blob == part + LOGO_HDR_LEN, "blob points past the header");
	CHECK(info.blob_len == sizeof(blob), "blob length from the header");
	CHECK(info.nframes == 1, "frame count from the BAN1 header");
	CHECK(info.hold_ms == 2500, "hold time from the header");
	CHECK(info.bg_rgb == 0x070b1e, "background colour packed as 0xRRGGBB");

	/* Erased flash -- the individual unit -- is not a logo. */
	memset(part, 0xff, sizeof(part));
	CHECK(!logo_parse(part, sizeof(part), 4, 3, &info),
	      "erased flash is absent");
	memset(part, 0x00, sizeof(part));
	CHECK(!logo_parse(part, sizeof(part), 4, 3, &info),
	      "zeroed flash is absent");

	build(part, sizeof(part));
	part[2] = 'X';
	CHECK(!logo_parse(part, sizeof(part), 4, 3, &info), "wrong magic");

	build(part, sizeof(part));
	wr16(part + 4, 2);
	CHECK(!logo_parse(part, sizeof(part), 4, 3, &info),
	      "a version this firmware does not know is absent");

	build(part, sizeof(part));
	part[LOGO_HDR_LEN + 14] ^= 0x01;	/* flip a pixel bit */
	CHECK(!logo_parse(part, sizeof(part), 4, 3, &info),
	      "a corrupt blob fails the CRC");

	build(part, sizeof(part));
	wr32(part + 8, 0xffffffffu);
	CHECK(!logo_parse(part, sizeof(part), 4, 3, &info),
	      "a length past the partition is refused before the CRC walks it");

	build(part, sizeof(part));
	wr32(part + 8, 4);
	wr32(part + 12, logo_crc32(blob, 4));
	CHECK(!logo_parse(part, sizeof(part), 4, 3, &info),
	      "a blob too short for a BAN1 header is refused");

	build(part, sizeof(part));
	CHECK(!logo_parse(part, sizeof(part), 320, 240, &info),
	      "a canvas that is not the panel's size is refused");

	CHECK(!logo_parse(part, LOGO_HDR_LEN + 4, 4, 3, &info),
	      "a partition too small to hold the blob is refused");
	CHECK(!logo_parse(NULL, sizeof(part), 4, 3, &info), "NULL is absent");

	printf("%d failure(s)\n", failures);
	return failures ? 1 : 0;
}
