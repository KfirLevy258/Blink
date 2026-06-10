#ifndef PROTO_H
#define PROTO_H

/* Initialize protocol I/O, send hello. Call once at startup. */
void proto_init(void);

/* Run one service iteration: drain RX lines (dispatch by "t") and emit a
 * periodic ping. Call repeatedly from the main loop. */
void proto_service(void);

#endif /* PROTO_H */
