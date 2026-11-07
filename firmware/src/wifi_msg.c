#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "wifi_msg.h"

static int nibble(char c)
{
	if (c >= '0' && c <= '9') {
		return c - '0';
	}
	if (c >= 'a' && c <= 'f') {
		return c - 'a' + 10;
	}
	if (c >= 'A' && c <= 'F') {
		return c - 'A' + 10;
	}
	return -1;
}

int wifi_hex_decode(const char *hex, uint8_t *out, size_t out_max)
{
	size_t n = strlen(hex);

	if ((n & 1) || n / 2 > out_max) {
		return -1;
	}
	for (size_t i = 0; i < n / 2; i++) {
		int hi = nibble(hex[2 * i]);
		int lo = nibble(hex[2 * i + 1]);

		if (hi < 0 || lo < 0) {
			return -1;
		}
		out[i] = (uint8_t)(hi << 4 | lo);
	}
	return (int)(n / 2);
}

bool wifi_psk_valid(const char *psk, size_t n)
{
	bool hex = n == 64;

	if (n < 8 || n > 64) {
		return false;
	}
	for (size_t i = 0; i < n; i++) {
		unsigned char c = (unsigned char)psk[i];

		if (nibble((char)c) < 0) {
			hex = false;
		}
		if (c < 0x20 || c > 0x7e) {
			return false;
		}
	}
	/* 64 characters pass only as a raw PSK. */
	return n < 64 || hex;
}

/* Appends printf-style at *pos; false once it no longer fits. */
static bool put(char *buf, size_t n, size_t *pos, const char *fmt, ...)
	__attribute__((format(printf, 4, 5)));

static bool put(char *buf, size_t n, size_t *pos, const char *fmt, ...)
{
	va_list ap;
	int r;

	if (*pos >= n) {
		return false;
	}
	va_start(ap, fmt);
	r = vsnprintf(buf + *pos, n - *pos, fmt, ap);
	va_end(ap);
	if (r < 0 || (size_t)r >= n - *pos) {
		return false;
	}
	*pos += (size_t)r;
	return true;
}

static bool put_hex(char *buf, size_t n, size_t *pos, const char *key,
		    const uint8_t *b, size_t len)
{
	if (!put(buf, n, pos, ",\"%s\":\"", key)) {
		return false;
	}
	for (size_t i = 0; i < len; i++) {
		if (!put(buf, n, pos, "%02x", b[i])) {
			return false;
		}
	}
	return put(buf, n, pos, "\"");
}

int wifi_fmt_net(char *buf, size_t n, int i, int count,
		 const struct net_link_ap *ap)
{
	static const char *const sec[] = { "open", "wpa2", "wpa3", "mixed" };
	size_t pos = 0;

	if (!put(buf, n, &pos, "{\"t\":\"wifi_net\",\"i\":%d,\"n\":%d", i, count)) {
		return -1;
	}
	if (ap != NULL) {
		if (!put_hex(buf, n, &pos, "s", ap->ssid, ap->ssid_len) ||
		    !put(buf, n, &pos, ",\"r\":%d,\"sec\":\"%s\"", ap->rssi,
			 sec[ap->sec & 3])) {
			return -1;
		}
	}
	return put(buf, n, &pos, "}") ? (int)pos : -1;
}

int wifi_fmt_state(char *buf, size_t n, const struct wifi_view *v)
{
	size_t pos = 0;
	bool ok = put(buf, n, &pos, "{\"t\":\"wifi_state\",\"st\":\"%s\",\"link\":\"%s\"",
		      v->st, v->link);

	if (ok && v->ssid != NULL && v->ssid_len > 0) {
		ok = put_hex(buf, n, &pos, "s", v->ssid, v->ssid_len);
	}
	if (ok && v->ip != NULL && v->ip[0]) {
		ok = put(buf, n, &pos, ",\"ip\":\"%s\"", v->ip);
	}
	if (ok && v->port != 0) {
		ok = put(buf, n, &pos, ",\"port\":%u", (unsigned)v->port);
	}
	if (ok && v->rssi != -127) {
		ok = put(buf, n, &pos, ",\"r\":%d", v->rssi);
	}
	if (ok && v->kid != NULL && v->kid[0]) {
		ok = put(buf, n, &pos, ",\"kid\":\"%s\"", v->kid);
	}
	if (ok && v->why != NULL && v->why[0]) {
		ok = put(buf, n, &pos, ",\"why\":\"%s\"", v->why);
	}
	return ok && put(buf, n, &pos, "}") ? (int)pos : -1;
}

bool wifi_state_changed(int last_st, int last_why, int last_link,
			int st, int why, int link)
{
	if (last_st == WIFI_LAST_NONE) {
		return false;
	}
	return last_st == WIFI_LAST_OWED || st != last_st ||
	       why != last_why || link != last_link;
}
