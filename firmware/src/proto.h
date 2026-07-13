#ifndef PROTO_H
#define PROTO_H

#include <stdbool.h>

/* Initialize protocol I/O, send hello. Call once at startup. */
void proto_init(void);

/* Run one service iteration: drain RX lines (dispatch by "t") and emit a
 * periodic ping. Call repeatedly from the main loop. */
void proto_service(void);

/* True once any protocol message has arrived from a PC daemon -- used at boot
 * to choose USB mode over WiFi. */
bool proto_host_seen(void);

#endif /* PROTO_H */
