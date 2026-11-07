/*
 * The four calls ota_net.c makes of the flash and of MCUboot, so that file
 * compiles on a laptop. On the board they ARE Zephyr's (zephyr/dfu/flash_img.h
 * and boot_request_upgrade); on the host the same names are declared here and
 * defined by tests/support/flash_img_fake.c, a RAM slot 1 that remembers what
 * was erased, what was written and whether an upgrade was requested. The
 * signatures are copied from Zephyr 4.3 and must stay identical to it.
 */
#ifndef FLASH_IMG_PORT_H
#define FLASH_IMG_PORT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __ZEPHYR__
#include <zephyr/dfu/flash_img.h>
#include <zephyr/dfu/mcuboot.h>
#else
#define BOOT_UPGRADE_TEST 0
struct flash_img_context {
	size_t written;		/* bytes on the fake flash */
	size_t buffered;	/* a partial block still in RAM */
	uint8_t buf[512];
};
int flash_img_init(struct flash_img_context *ctx);
int flash_img_buffered_write(struct flash_img_context *ctx, const uint8_t *data,
			     size_t len, bool flush);
size_t flash_img_bytes_written(struct flash_img_context *ctx);
int boot_request_upgrade(int permanent);
#endif

#endif /* FLASH_IMG_PORT_H */
