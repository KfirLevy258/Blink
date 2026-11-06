/* flash_img_port.h on a laptop: a RAM slot 1 with MCUboot's trailer at the
 * top, and a record of what was done to it. Slot 1 is 1376256 bytes; the last
 * 57344 (the padding sector and the trailer, 1376256 - OTA_IMAGE_MAX) are
 * where a daemon must never be able to write. NOT Zephyr's code: only its
 * shape (a 512-byte block buffer, flush on request, bytes_written counting
 * the buffered tail). Never link it into firmware. */
#include <stdbool.h>
#include <string.h>

#include "flash_img_port.h"

#define SLOT1_SIZE 1376256u
#define SECTOR 4096u

uint8_t fake_slot1[SLOT1_SIZE];
int fake_erases;		/* sectors erased */
int fake_upgrade_requests;
int fake_upgrade_arg = -1;
int fake_fail_write;		/* next buffered write returns -5 and clears */
bool fake_flash_inited;
static uint32_t erased_to;	/* sectors below this are erased (progressive) */

void fake_flash_reset(void)
{
	memset(fake_slot1, 0xff, sizeof(fake_slot1));
	fake_erases = 0;
	fake_upgrade_requests = 0;
	fake_upgrade_arg = -1;
	fake_fail_write = 0;
	erased_to = 0;
}

int flash_img_init(struct flash_img_context *ctx)
{
	memset(ctx, 0, sizeof(*ctx));
	fake_flash_inited = true;
	return 0;
}

static void put(struct flash_img_context *ctx, const uint8_t *p, size_t n)
{
	/* Progressive erase, as CONFIG_IMG_ERASE_PROGRESSIVELY does: a sector is
	 * erased the first time a write reaches it. */
	while (erased_to < ctx->written + n) {
		fake_erases++;
		erased_to += SECTOR;
	}
	memcpy(fake_slot1 + ctx->written, p, n);
	ctx->written += n;
}

int flash_img_buffered_write(struct flash_img_context *ctx, const uint8_t *data,
			     size_t len, bool flush)
{
	if (fake_fail_write) {
		fake_fail_write = 0;
		return -5;
	}
	while (len > 0) {
		size_t room = sizeof(ctx->buf) - ctx->buffered;
		size_t k = len < room ? len : room;

		memcpy(ctx->buf + ctx->buffered, data, k);
		ctx->buffered += k;
		data += k;
		len -= k;
		if (ctx->buffered == sizeof(ctx->buf)) {
			put(ctx, ctx->buf, ctx->buffered);
			ctx->buffered = 0;
		}
	}
	if (flush && ctx->buffered > 0) {
		put(ctx, ctx->buf, ctx->buffered);
		ctx->buffered = 0;
	}
	return 0;
}

size_t flash_img_bytes_written(struct flash_img_context *ctx)
{
	return ctx->written + ctx->buffered;
}

int boot_request_upgrade(int permanent)
{
	fake_upgrade_requests++;
	fake_upgrade_arg = permanent;
	return 0;
}
