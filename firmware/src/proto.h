#ifndef PROTO_H
#define PROTO_H

#include "ota_parse.h"

#include <stdbool.h>

/* Initialize protocol I/O, send hello. Call once at startup. */
void proto_init(void);

/* Run one service iteration: drain RX lines (dispatch by "t") and emit a
 * periodic ping. Call repeatedly from the main loop. */
void proto_service(void);

/* True once any protocol message has arrived from a PC daemon -- used at boot
 * to choose USB mode over WiFi. */
bool proto_host_seen(void);

/* The daemon's release version as it announced itself, or "" if it has not.
 * Firmware and daemon ship from one tag, so this should equal
 * CLAUGE_FW_VERSION on a fully updated machine. */
const char *proto_host_version(void);

/* True when that version is older than this firmware -- i.e. the app on the
 * computer is the half that is behind. Advisory only. */
bool proto_host_outdated(void);

/* Re-announce to the host (fresh hello). Called when the gauge screen becomes
 * ready: the daemon answers hello with an immediate time+usage push, so the
 * screen fills right away instead of at its next 60 s poll. */
void proto_resync(void);

/*
 * OTA over this link, for USB-bridge mode -- which has no network of its own,
 * so ota.c's HTTPS path is unreachable there. The daemon fetches the release
 * and writes it with esptool over this same cable. The board only consents.
 * See the block comment in proto.c.
 */
void proto_ota_check(void);			/* ask the daemon what it has */
/* The daemon version the current offer also carries, or "" -- one tap installs
 * both halves, so the confirmation screen says so. */
const char *proto_ota_app_version(void);
bool proto_ota_install(void);			/* approve; the daemon flashes */

#endif /* PROTO_H */
