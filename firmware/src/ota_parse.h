#ifndef OTA_PARSE_H
#define OTA_PARSE_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

/* Pure parsing/compare helpers for the OTA stream. No Zephyr dependencies:
 * host-tested in tests/ota_parse/. */

struct ota_manifest {
	char version[16];	/* "0.4.0" */
	uint32_t size;		/* bytes of blink-fw.bin */
	char sha256[65];	/* lowercase hex + NUL */
};

int ota_parse_manifest(const char *buf, size_t len, struct ota_manifest *out);
bool ota_version_newer(const char *cand, const char *cur);

/* Slot 1's usable size under swap-move: slot 1 (1376256) minus the padding
 * sector (4096) minus the sector-aligned trailer (53248), Task 0.3's number
 * from the deployed MCUboot configuration (spec S9, D32). A chunk past it
 * could reach MCUboot's trailer, and a trailer with image_ok set makes a swap
 * permanent and skips the test boot. */
#define OTA_IMAGE_MAX 1318912u
/* The board acks whenever the accepted count crosses a multiple of this. */
#define OTA_ACK_EVERY 16384u

enum ota_hdr_verdict {
	OTA_HDR_OK,
	OTA_HDR_BAD_MAGIC,	/* too short, or not an MCUboot header */
	OTA_HDR_NOT_NEWER,	/* not strictly newer than the running image */
	OTA_HDR_NOT_OFFERED,	/* newer, but not the version consented to */
};

/* The first chunk's MCUboot image header against the running version and the
 * version in the ota_avail the owner consented to. MCUboot's own downgrade
 * prevention is unusable: the bootloader on shipped boards cannot be updated. */
enum ota_hdr_verdict ota_hdr_check(const uint8_t *hdr, size_t n,
				   const char *running, const char *offered);
/* No gap, no rewind, and inside the image. */
bool ota_chunk_in_bounds(uint32_t got, uint32_t off, size_t n);
/* The count crossed a 16 KiB boundary, or reached the manifest size (D45:
 * an image that is not a multiple of 16 KiB still gets its last ack). */
bool ota_net_should_ack(uint32_t before, uint32_t after, uint32_t size);
bool ota_net_should_seal(uint32_t got, uint32_t size);

#endif /* OTA_PARSE_H */
