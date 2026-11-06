/*
 * The handshake and discovery bytes of spec S6.2 and S6.4, pure: no socket,
 * no clock, no printk. Written in the RAM spike (Task 0.2), tested and given
 * its error paths in Task 2.3.
 *
 *   M1  board -> PC  20 B  "BLW" 0x01 || Nb[16]
 *   M2  PC -> board  48 B  Np[16] || HMAC(K, "blink-wifi-1 pc" || Nb || Np)
 *   M3  board -> PC  32 B  HMAC(K, "blink-wifi-1 board" || Nb || Np)
 *   keys = HKDF-SHA256(salt = Nb || Np, ikm = K,
 *                      info = "blink-wifi-1 session", L = 64)
 *   k_pb = keys[0:32] (PC -> board), k_bp = keys[32:64] (board -> PC)
 *
 *   query   36 B  "BLQ" 0x01 || Nq[16] || HMAC(K, "blink-disc-1 q" || Nq)[0:16]
 *   answer  22 B  "BLA" 0x01 || HMAC(K, "blink-disc-1 a" || Nq || port[2])[0:16]
 *                 || port[2]          (port big-endian)
 */
#ifndef WL_HS_H
#define WL_HS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define WL_M1_LEN 20
#define WL_M2_LEN 48
#define WL_M3_LEN 32
#define WL_Q_LEN 36
#define WL_A_LEN 22

struct wl_hs {
	uint8_t nb[16];
	uint8_t np[16];
};

/* Draws a fresh Nb into hs. 0, or -1 if wl_random failed (send nothing). */
int wl_hs_m1(struct wl_hs *hs, uint8_t out[WL_M1_LEN]);
/* 0 ok (Np kept in hs), -1 bad tag or length, -2 crypto error. */
int wl_hs_check_m2(struct wl_hs *hs, const uint8_t key[32], const uint8_t *m2,
		   size_t n);
/* 0 or -2. */
int wl_hs_m3(const struct wl_hs *hs, const uint8_t key[32],
	     uint8_t out[WL_M3_LEN]);
/* 0 or -2; on -2 both outputs are zeroed. */
int wl_hs_keys(const struct wl_hs *hs, const uint8_t key[32],
	       uint8_t k_pb[32], uint8_t k_bp[32]);
void wl_hs_wipe(struct wl_hs *hs);

/* WL_A_LEN with the answer in out, or -1: send nothing. */
int wl_disc_answer(const uint8_t key[32], const uint8_t *q, size_t n,
		   uint16_t port, uint8_t out[WL_A_LEN]);

#endif /* WL_HS_H */
