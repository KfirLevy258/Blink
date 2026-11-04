/*
 * A PNG writer with no library: stored (uncompressed) deflate blocks, so all
 * it needs is a CRC-32 and an Adler-32. A 320 x 240 screen is 230 KB this
 * way -- fine for a bench render, and it keeps the harness free of zlib.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "harness.h"

static uint32_t crc_table[256];

static void crc_init(void)
{
	for (uint32_t n = 0; n < 256; n++) {
		uint32_t c = n;

		for (int k = 0; k < 8; k++) {
			c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
		}
		crc_table[n] = c;
	}
}

static uint32_t crc(uint32_t c, const uint8_t *p, size_t n)
{
	c ^= 0xFFFFFFFFu;
	while (n--) {
		c = crc_table[(c ^ *p++) & 0xFF] ^ (c >> 8);
	}
	return c ^ 0xFFFFFFFFu;
}

static void be32(uint8_t *p, uint32_t v)
{
	p[0] = (uint8_t)(v >> 24);
	p[1] = (uint8_t)(v >> 16);
	p[2] = (uint8_t)(v >> 8);
	p[3] = (uint8_t)v;
}

static void chunk(FILE *f, const char *type, const uint8_t *data, size_t n)
{
	uint8_t hdr[8];
	uint32_t c;

	be32(hdr, (uint32_t)n);
	memcpy(hdr + 4, type, 4);
	fwrite(hdr, 1, 8, f);
	if (n) {
		fwrite(data, 1, n, f);
	}
	c = crc(0, (const uint8_t *)type, 4);
	c = crc(c, data, n);
	be32(hdr, c);
	fwrite(hdr, 1, 4, f);
}

int png_write(const char *path, const uint8_t *rgb, int w, int h)
{
	static const uint8_t sig[8] = { 137, 'P', 'N', 'G', 13, 10, 26, 10 };
	size_t row = (size_t)w * 3 + 1;
	size_t raw_n = row * h;
	size_t blocks = (raw_n + 65534) / 65535;
	size_t z_n = 2 + raw_n + blocks * 5 + 4;
	uint8_t *raw = malloc(raw_n), *z = malloc(z_n), ihdr[13];
	uint32_t a = 1, b = 0;
	size_t o = 0;
	FILE *f;

	if (!raw || !z) {
		free(raw);
		free(z);
		return -1;
	}
	crc_init();
	for (int y = 0; y < h; y++) {
		raw[y * row] = 0;	/* filter: none */
		memcpy(raw + y * row + 1, rgb + (size_t)y * w * 3, (size_t)w * 3);
	}
	z[o++] = 0x78;
	z[o++] = 0x01;
	for (size_t i = 0; i < raw_n; i += 65535) {
		size_t n = raw_n - i < 65535 ? raw_n - i : 65535;

		z[o++] = (i + n == raw_n) ? 1 : 0;
		z[o++] = (uint8_t)n;
		z[o++] = (uint8_t)(n >> 8);
		z[o++] = (uint8_t)~n;
		z[o++] = (uint8_t)(~n >> 8);
		memcpy(z + o, raw + i, n);
		o += n;
	}
	for (size_t i = 0; i < raw_n; i++) {
		a = (a + raw[i]) % 65521;
		b = (b + a) % 65521;
	}
	be32(z + o, (b << 16) | a);
	o += 4;

	be32(ihdr, (uint32_t)w);
	be32(ihdr + 4, (uint32_t)h);
	ihdr[8] = 8;		/* bit depth */
	ihdr[9] = 2;		/* RGB */
	ihdr[10] = ihdr[11] = ihdr[12] = 0;

	f = fopen(path, "wb");
	if (!f) {
		free(raw);
		free(z);
		return -1;
	}
	fwrite(sig, 1, 8, f);
	chunk(f, "IHDR", ihdr, 13);
	chunk(f, "IDAT", z, o);
	chunk(f, "IEND", NULL, 0);
	fclose(f);
	free(raw);
	free(z);
	return 0;
}
