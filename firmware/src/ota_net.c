/*
 * The stream half of the OTA engine: hash and write one chunk at a time into
 * slot 1, then check the whole and hand it to MCUboot. It was ota.c's
 * stream_consume/stream_seal, which the HTTPS download fed; it is fed by O
 * frames now (spec S9) and includes nothing of Zephyr, so a laptop runs it.
 */
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include "ota_net.h"
#include "ota_parse.h"
#include "flash_img_port.h"
#include "wl_crypto.h"
#include "version.h"	/* BLINK_FW_VERSION: a plain define, no Zephyr */

#ifdef __ZEPHYR__
#include <zephyr/sys/printk.h>
#else
#define printk printf
#endif

static struct flash_img_context fictx;
static bool open_;
static uint32_t got, total;
static struct ota_manifest man;	/* begin's copy: the seal judges THIS, never a later ota_avail */

bool ota_net_active(void)
{
	return open_;
}

void ota_net_abort(void)
{
	if (open_) {
		/* Slot 1 keeps whatever was written. It is never marked pending
		 * (the trailer is out of reach of every chunk), so MCUboot
		 * ignores it. */
		printk("[ota] stream aborted\n");
	}
	open_ = false;
	got = total = 0;
}

int ota_net_begin(const struct ota_manifest *m)
{
	if (ota_unconfirmed()) {
		return -EBUSY;
	}
	if (m->size == 0 || m->size > OTA_IMAGE_MAX) {
		return -EINVAL;
	}
	open_ = false;
	if (flash_img_init(&fictx) != 0 || wl_sha256_start() != 0) {
		return -EIO;
	}
	man = *m;
	total = m->size;
	got = 0;
	open_ = true;
	printk("[ota] downloading\n");
	return 0;
}

int ota_net_chunk(uint32_t off, const uint8_t *b, size_t n)
{
	if (!open_) {
		return -1;
	}
	if (!ota_chunk_in_bounds(got, off, n) || n > total - got ||
	    (got == 0 && ota_hdr_check(b, n, BLINK_FW_VERSION, man.version) != OTA_HDR_OK)) {
		ota_net_abort();
		return -1;
	}
	if (wl_sha256_update(b, n) != 0) {
		ota_net_abort();
		return -1;
	}
	int err = flash_img_buffered_write(&fictx, b, n, false);

	if (err) {
		printk("[ota] slot1 write failed: %d\n", err);
		ota_net_abort();
		return -1;
	}
	got += (uint32_t)n;
	return (int)got;
}

enum ota_result ota_net_end(void)
{
	if (!open_) {
		return OTA_ERR_SIZE;
	}
	if (!ota_net_should_seal(got, man.size)) {
		printk("[ota] truncated\n");
		ota_net_abort();
		return OTA_ERR_SIZE;
	}
	/* The writer keeps a partial block in RAM until this flush. */
	if (flash_img_buffered_write(&fictx, NULL, 0, true) != 0) {
		ota_net_abort();
		return OTA_ERR_FLASH;
	}
	if (flash_img_bytes_written(&fictx) != man.size) {
		printk("[ota] truncated\n");
		ota_net_abort();
		return OTA_ERR_SIZE;
	}

	uint8_t digest[32];
	char hex[65];

	if (wl_sha256_finish(digest) != 0) {
		ota_net_abort();
		return OTA_ERR_HASH;
	}
	for (int i = 0; i < 32; i++) {
		snprintf(&hex[i * 2], 3, "%02x", digest[i]);
	}
	if (strcmp(hex, man.sha256) != 0) {
		printk("[ota] sha256 mismatch\n");
		ota_net_abort();
		return OTA_ERR_HASH;
	}

	open_ = false;
	/* Slot 1 now holds verified bytes. Anything failing before this line
	 * leaves it garbage, which is harmless: never marked pending. */
	if (boot_request_upgrade(BOOT_UPGRADE_TEST) != 0) {
		return OTA_ERR_FLASH;
	}
	printk("[ota] verified and marked pending\n");
	return OTA_OK;
}

void ota_net_service(bool usb_takeover, bool session_up, bool replaced)
{
	if (open_ && (usb_takeover || !session_up || replaced)) {
		ota_net_abort();
	}
}
