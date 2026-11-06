#ifndef WIFI_MSG_H
#define WIFI_MSG_H
/*
 * The wifi_* lines (spec S6.1): parsing a hex field and building the two
 * board-to-host lines. Free of Zephyr, so tests/wifi_msg/host_test.c runs
 * them on a laptop. SSIDs are bytes and passwords are any printable
 * character, which is why both travel as hex (no JSON escaping here).
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "net_link.h"

/* Hex (either case) to bytes: the count, or -1 on an odd length, a
 * non-hex digit, or more than out_max bytes. */
int wifi_hex_decode(const char *hex, uint8_t *out, size_t out_max);

/* WPA2's rule: 8 to 63 printable ASCII, or exactly 64 hex digits. */
bool wifi_psk_valid(const char *psk, size_t n);

/* One wifi_net line. ap == NULL with count == 0 is the end marker of an
 * empty scan: {"t":"wifi_net","i":0,"n":0}. Length, or -1 if it did not
 * fit in n. */
int wifi_fmt_net(char *buf, size_t n, int i, int count,
		 const struct net_link_ap *ap);

/* Absent fields are left out: ssid NULL or 0 long, ip/kid/why NULL or "",
 * port 0, rssi -127. */
struct wifi_view {
	const char *st;
	const char *link;
	const uint8_t *ssid;
	size_t ssid_len;
	const char *ip;
	uint16_t port;
	int8_t rssi;
	const char *kid;
	const char *why;
};

int wifi_fmt_state(char *buf, size_t n, const struct wifi_view *v);

#endif /* WIFI_MSG_H */
