/* wifi_msg.c on a laptop: hex fields, the password rule, and the two
 * board-to-host lines at their widest (spec S6.1, budget 512 B).
 *
 *   cc -I firmware/src tests/wifi_msg/host_test.c firmware/src/wifi_msg.c \
 *      firmware/src/msg_parse.c -o /tmp/t && /tmp/t
 */
#include <stdio.h>
#include <string.h>

#include "msg_parse.h"
#include "wifi_msg.h"

static int fails;

static void check(const char *name, int ok)
{
	printf("%s %s\n", ok ? "PASS" : "FAIL", name);
	fails += !ok;
}

int main(void)
{
	uint8_t b[8];
	char line[512];
	char t[24], s[80];

	/* An SSID that is not UTF-8 (review focus 4). */
	check("hex_ssid_any_bytes", wifi_hex_decode("ff61e29883", b, 8) == 5 &&
	      b[0] == 0xff && b[1] == 0x61 && b[2] == 0xe2 && b[3] == 0x98 &&
	      b[4] == 0x83);
	check("hex_odd_length_refused", wifi_hex_decode("abc", b, 8) == -1);
	check("hex_non_hex_refused", wifi_hex_decode("zz", b, 8) == -1 &&
	      wifi_hex_decode("0g", b, 8) == -1);
	check("hex_over_max_refused", wifi_hex_decode("0102030405", b, 4) == -1 &&
	      wifi_hex_decode("01020304", b, 4) == 4);

	/* Task 1.3's table. */
	{
		char p63[64], p64h[65], p64x[65];

		memset(p63, 'a', 63); p63[63] = 0;
		memset(p64h, 'f', 64); p64h[64] = 0;
		memset(p64x, 'f', 64); p64x[63] = 'g'; p64x[64] = 0;
		check("psk_rules",
		      !wifi_psk_valid("1234567", 7) && wifi_psk_valid("12345678", 8) &&
		      wifi_psk_valid(p63, 63) && wifi_psk_valid(p64h, 64) &&
		      !wifi_psk_valid(p64x, 64) &&
		      wifi_psk_valid("pa\"ss\\ word ~", 12) &&
		      !wifi_psk_valid("pass\tword", 9) &&
		      !wifi_psk_valid("pass\x7fword", 9) &&
		      !wifi_psk_valid("pass\xc3\xa9word", 10));
	}

	{
		struct net_link_ap ap = { .ssid_len = 32, .rssi = -100,
					  .sec = NL_SEC_MIXED };
		int n;

		memset(ap.ssid, 0xff, 32);
		n = wifi_fmt_net(line, sizeof(line), 19, 20, &ap);
		check("net_line_fits", n > 0 && n < 511 && (int)strlen(line) == n &&
		      msg_get_str(line, "t", t, sizeof(t)) &&
		      strcmp(t, "wifi_net") == 0 &&
		      msg_get_str(line, "s", s, sizeof(s)) && strlen(s) == 64 &&
		      msg_get_str(line, "sec", t, sizeof(t)) &&
		      strcmp(t, "mixed") == 0);
		check("net_line_too_small_refused",
		      wifi_fmt_net(line, 20, 19, 20, &ap) == -1);
		n = wifi_fmt_net(line, sizeof(line), 0, 0, NULL);
		check("net_empty_scan_marker", n > 0 &&
		      strcmp(line, "{\"t\":\"wifi_net\",\"i\":0,\"n\":0}") == 0);
	}

	{
		uint8_t ssid[32];
		struct wifi_view v = { "joining", "usb", ssid, 32,
				       "255.255.255.255", 65535, -100,
				       "deadbeef",
				       "WPA3-only networks are not supported" };
		int n;

		memset(ssid, 0xff, 32);
		n = wifi_fmt_state(line, sizeof(line), &v);
		check("state_line_fits", n > 0 && n < 511 &&
		      msg_get_str(line, "kid", t, sizeof(t)) &&
		      strcmp(t, "deadbeef") == 0 &&
		      msg_get_str(line, "why", s, sizeof(s)));

		struct wifi_view e = { "off", "usb", NULL, 0, NULL, 0, -127,
				       NULL, "" };

		n = wifi_fmt_state(line, sizeof(line), &e);
		check("state_omits_absent_fields", n > 0 &&
		      strcmp(line, "{\"t\":\"wifi_state\",\"st\":\"off\","
			     "\"link\":\"usb\"}") == 0);
	}
	return fails != 0;
}
