/*
 * The session frame of spec S6.3, pure. Written in the RAM spike (Task 0.2),
 * tested and given its error paths in Task 2.3.
 *
 *   frame     = len[2, BE] || ciphertext[len] || tag[16]
 *   len       = plaintext length, 1 <= len <= WL_PT_MAX
 *   nonce     = 0x00000000 || counter[8, BE]   (one key and counter per
 *               direction, both from 0, strictly in order)
 *   aad       = len[2]
 *   plaintext = type[1] || body
 */
#ifndef WL_FRAME_H
#define WL_FRAME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define WL_PT_MAX 517
#define WL_NONCE_LEN 12
#define WL_TAG_LEN 16
#define WL_FRAME_MAX (2 + WL_PT_MAX + 16)
#define WL_T_LINE 'L'
#define WL_T_OTA 'O'
#define WL_T_LOGO 'G'

struct wl_dir {
	uint8_t key[32];
	uint64_t ctr;
	bool dead;
};

/* Frame length written to out, or -1; -1 sets dead. */
int wl_frame_seal(struct wl_dir *d, uint8_t type, const uint8_t *body,
		  size_t n, uint8_t *out, size_t out_max);
/* The ciphertext length a 2-byte header announces, or -1 if 0 or over
 * WL_PT_MAX: the reader closes without reading the body. */
int wl_frame_len(const uint8_t hdr[2]);
/* Body length, with the type in *type, or -1; -1 sets dead. A type other
 * than L, O or G is refused like a bad tag, as pc/wifi_session.py does. */
int wl_frame_open(struct wl_dir *d, const uint8_t *frame, size_t n,
		  uint8_t *type, uint8_t *body, size_t body_max);
void wl_dir_wipe(struct wl_dir *d);

#endif /* WL_FRAME_H */
