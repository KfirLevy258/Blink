/*
 * The logo partition written from a stream. See logo_write.h. Pure C above
 * the port, so tests/logo_write/host_test.c runs it on a RAM partition.
 */
#include <errno.h>

#include "logo_write.h"
#include "logo_parse.h"

#define LOGO_W 320	/* the panel, as logo.c wants it */
#define LOGO_H 240

static bool open_;
static uint32_t got, total, want_crc;
static enum { ST_UNKNOWN, ST_PRESENT, ST_ABSENT } st;
static uint32_t st_bytes;
static uint16_t st_frames;

/* An erase that failed leaves the partition in a state nobody knows, so the
 * next logo_state parses it again rather than say "none". */
static int erase(void)
{
	int rc = logo_part_erase();

	st = rc == 0 ? ST_ABSENT : ST_UNKNOWN;
	return rc;
}

static void drop(void)
{
	open_ = false;
	(void)erase();
}

bool logo_write_active(void)
{
	return open_;
}

int logo_write_begin(uint32_t size, uint32_t crc)
{
	if (size == 0 || size > LOGO_IMAGE_MAX) {
		return -EINVAL;
	}
	open_ = false;
	if (erase() != 0) {
		return -EIO;
	}
	total = size;
	want_crc = crc;
	got = 0;
	open_ = true;
	return 0;
}

int logo_write_chunk(uint32_t off, const uint8_t *b, size_t n)
{
	if (!open_) {
		return -1;
	}
	if (off != got || n == 0 || n > total - got ||
	    logo_part_write(off, b, n) != 0) {
		drop();
		return -1;
	}
	got += (uint32_t)n;
	return (int)got;
}

/* Parse what the partition holds into st. The caller has it mapped. */
static bool judge(const uint8_t *map)
{
	struct logo_info info;

	if (map != NULL && logo_parse(map, LOGO_IMAGE_MAX, LOGO_W, LOGO_H, &info)) {
		st = ST_PRESENT;
		st_bytes = LOGO_HDR_LEN + (uint32_t)info.blob_len;
		st_frames = info.nframes;
		return true;
	}
	st = ST_ABSENT;
	return false;
}

int logo_write_end(void)
{
	if (!open_) {
		return -1;
	}
	open_ = false;
	if (got != total) {
		(void)erase();
		return -1;
	}

	const uint8_t *map = logo_part_map();
	/* The stream's own CRC first: logo_parse's covers the blob, this one
	 * every byte that was sent, header included. */
	bool ok = map != NULL && logo_crc32(map, total) == want_crc && judge(map);

	if (map != NULL) {
		logo_part_unmap();
	}
	if (!ok) {
		(void)erase();
		return -1;
	}
	return 0;
}

void logo_write_abort(void)
{
	if (open_) {
		drop();
	}
}

int logo_write_remove(void)
{
	open_ = false;
	return erase();
}

bool logo_write_state(uint32_t *bytes, uint16_t *frames)
{
	if (st == ST_UNKNOWN) {
		const uint8_t *map = logo_part_map();

		(void)judge(map);
		if (map != NULL) {
			logo_part_unmap();
		}
	}
	if (st != ST_PRESENT) {
		return false;
	}
	*bytes = st_bytes;
	*frames = st_frames;
	return true;
}

#ifdef __ZEPHYR__
/*
 * The port on the board. flash_area_write through the ESP32 driver is what
 * the OTA stream already relies on: with flash encryption on, the driver
 * encrypts and handles alignment itself (flash_esp32.c aligned_flash_write),
 * and the memory-mapped read below decrypts through the cache exactly as
 * logo.c's does at boot, so nothing here knows whether the chip is fused.
 * esp_flash_write and _erase flush the cache over what they touched, so the
 * map after a write reads the new bytes.
 */
#include <zephyr/kernel.h>
#include <zephyr/storage/flash_map.h>
#include <spi_flash_mmap.h>

BUILD_ASSERT(FIXED_PARTITION_SIZE(logo_partition) == LOGO_IMAGE_MAX,
	     "LOGO_IMAGE_MAX must be the logo partition's size");

static spi_flash_mmap_handle_t map_handle;

int logo_part_erase(void)
{
	const struct flash_area *fa;
	int rc = flash_area_open(FIXED_PARTITION_ID(logo_partition), &fa);

	if (rc == 0) {
		rc = flash_area_erase(fa, 0, LOGO_IMAGE_MAX);
		flash_area_close(fa);
	}
	return rc;
}

int logo_part_write(uint32_t off, const uint8_t *b, size_t n)
{
	const struct flash_area *fa;
	int rc = flash_area_open(FIXED_PARTITION_ID(logo_partition), &fa);

	if (rc == 0) {
		rc = flash_area_write(fa, off, b, n);
		flash_area_close(fa);
	}
	return rc;
}

const uint8_t *logo_part_map(void)
{
	const void *mem = NULL;

	if (spi_flash_mmap(FIXED_PARTITION_OFFSET(logo_partition), LOGO_IMAGE_MAX,
			   SPI_FLASH_MMAP_DATA, &mem, &map_handle) != 0) {
		return NULL;
	}
	return mem;
}

void logo_part_unmap(void)
{
	spi_flash_munmap(map_handle);
}
#endif /* __ZEPHYR__ */
