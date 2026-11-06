#ifndef OTA_NET_H
#define OTA_NET_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include "ota.h"
#include "ota_parse.h"

/*
 * The board's half of a firmware image over the WiFi session (spec S9): the
 * bytes arrive as O frames, are hashed and written into slot 1 in order, and
 * on ota_end are checked and handed to MCUboot as a test image.
 *
 * MAIN THREAD ONLY. The network thread moves bytes into the ring and nothing
 * else; the hash and the flash writes, which run with caches off, stay here
 * (the Phase 0 spike proved this split). No locking.
 *
 * Never logs a byte or an offset of the image (CLAUDE.md).
 */

/* main.c: the running image is still in its test boot, so slot 1 holds the
 * only way back and a second image would erase it (D38). The host test
 * supplies its own. */
bool ota_unconfirmed(void);

/* -EBUSY while ota_unconfirmed(); -EINVAL if m->size is 0 or over
 * OTA_IMAGE_MAX; -EIO if the flash context or the hash cannot start; else 0,
 * with any stream already open dropped first. The caller then sends
 * ota_ack off:0. */
int ota_net_begin(const struct ota_manifest *m);

/* One chunk. The bytes accepted so far, or -1: out of order, past the image
 * or the manifest, a first chunk whose header is not a newer, offered MCUboot
 * image, or a flash error. Every -1 has already dropped the stream. */
int ota_net_chunk(uint32_t off, const uint8_t *b, size_t n);

/* ota_end: flush, length, hash, then boot_request_upgrade(TEST). OTA_OK only
 * when all agree; anything else drops the stream, and slot 1 is garbage that
 * was never marked pending. */
enum ota_result ota_net_end(const struct ota_manifest *m);

/* Drops the flash context and the hash. Seals and requests nothing. */
void ota_net_abort(void);
bool ota_net_active(void);

/* proto_service calls this once per pass, and it is the production trigger
 * for every abort: a stream is open and any flag says it cannot go on.
 * usb_takeover is link_wifi_must_close(now), session_up is
 * wl_server_session_up(), replaced is "wl_server_session_gen() is not the one
 * the stream began under". */
void ota_net_service(bool usb_takeover, bool session_up, bool replaced);

#endif /* OTA_NET_H */
