/*
 * Find the company logo, if this unit has one. See logo.h and logo_parse.h.
 */
#include <zephyr/kernel.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/printk.h>
#include <spi_flash_mmap.h>

#include "logo.h"

#define LOGO_W 320
#define LOGO_H 240

const struct logo_info *logo_active(void)
{
	static struct logo_info info;
	static enum { UNREAD, PRESENT, ABSENT } state = UNREAD;

	if (state != UNREAD) {
		return state == PRESENT ? &info : NULL;
	}

	/*
	 * Memory-map the whole partition and keep the mapping for the life of
	 * the boot. The flash cache decrypts on a fused chip exactly as it
	 * does for the app's own rodata, so a logo written encrypted at the
	 * factory reads back as the bytes the tool produced; a partition that
	 * was never written reads as noise there, and as 0xFF on a plaintext
	 * chip. logo_parse rejects both.
	 */
	const void *mem = NULL;
	spi_flash_mmap_handle_t handle;
	int rc = spi_flash_mmap(FIXED_PARTITION_OFFSET(logo_partition),
				FIXED_PARTITION_SIZE(logo_partition),
				SPI_FLASH_MMAP_DATA, &mem, &handle);

	if (rc != 0 || mem == NULL) {
		printk("[boot] logo: cannot map partition (%d)\n", rc);
		state = ABSENT;
		return NULL;
	}

	if (logo_parse(mem, FIXED_PARTITION_SIZE(logo_partition),
		       LOGO_W, LOGO_H, &info)) {
		state = PRESENT;
		printk("[boot] logo: company, %u bytes, %u frames, hold %u ms\n",
		       (unsigned)info.blob_len, info.nframes, info.hold_ms);
		return &info;
	}

	spi_flash_munmap(handle);
	state = ABSENT;
	printk("[boot] logo: none\n");
	return NULL;
}
