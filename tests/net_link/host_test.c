/*
 * The scan table behind wifi_net (spec S6.1): one entry per network name, at
 * its strongest reading, strongest first, at most 20.
 *
 *   cc -I firmware/src -o /tmp/t tests/net_link/host_test.c && /tmp/t
 *
 * (tests/ci/check_host_tests.sh runs this; the line above is for editing it.)
 *
 * The same name arrives once per access point and channel -- a mesh router is
 * three or four of them -- and an apartment block's air holds more than 20
 * networks, so a table that kept the first twenty it heard, or each mesh node
 * separately, would show the owner a list missing their own network.
 */
#include <stdio.h>
#include <string.h>

#include "net_link.h"

static int fails;

static void eq(const char *what, int got, int want)
{
	if (got != want) {
		fails++;
		printf("FAIL: %s (got %d, want %d)\n", what, got, want);
	} else {
		printf("PASS: %s\n", what);
	}
}

static void add(struct net_link_ap *t, int *n, const char *ssid, int rssi,
		int sec)
{
	net_link_scan_add(t, n, (const uint8_t *)ssid, strlen(ssid),
			  (int8_t)rssi, (uint8_t)sec);
}

static int find(const struct net_link_ap *t, int n, const char *ssid)
{
	for (int i = 0; i < n; i++) {
		if (t[i].ssid_len == strlen(ssid) &&
		    memcmp(t[i].ssid, ssid, t[i].ssid_len) == 0) {
			return i;
		}
	}
	return -1;
}

int main(void)
{
	struct net_link_ap t[NET_LINK_SCAN_MAX];
	int n = 0;
	char name[8];

	/* --- the same name, three access points ------------------------ */
	add(t, &n, "home", -80, NL_SEC_WPA2);
	add(t, &n, "home", -50, NL_SEC_MIXED);
	add(t, &n, "home", -70, NL_SEC_WPA2);
	eq("a name is kept once", n, 1);
	eq("at its strongest reading", t[0].rssi, -50);
	eq("with that reading's security", t[0].sec, NL_SEC_MIXED);

	/* --- what is not a name --------------------------------------- */
	net_link_scan_add(t, &n, (const uint8_t *)"", 0, -30, NL_SEC_WPA2);
	eq("a hidden network is not listed", n, 1);
	net_link_scan_add(t, &n, (const uint8_t *)"x", 33, -30, NL_SEC_WPA2);
	eq("a name over 32 bytes is not listed", n, 1);
	/* SSIDs are bytes, not strings: a prefix is another network. */
	add(t, &n, "hom", -60, NL_SEC_WPA2);
	eq("a prefix of a name is its own network", n, 2);

	/* --- a full table: twenty, and the weakest gives way ---------- */
	n = 0;
	for (int i = 0; i < 25; i++) {
		snprintf(name, sizeof(name), "n%02d", i);
		/* n00 is the weakest at -99, n24 the strongest at -75 */
		add(t, &n, name, -99 + i, NL_SEC_WPA2);
	}
	eq("at most twenty", n, NET_LINK_SCAN_MAX);
	eq("the strongest newcomer is in", find(t, n, "n24") >= 0, 1);
	eq("the weakest five are out", find(t, n, "n04") >= 0, 0);
	eq("the sixth weakest stays", find(t, n, "n05") >= 0, 1);
	add(t, &n, "weak", -99, NL_SEC_WPA2);
	eq("a newcomer weaker than all twenty is not let in",
	   find(t, n, "weak"), -1);
	add(t, &n, "n05", -40, NL_SEC_WPA2);
	eq("a name already in, heard stronger, is raised, not added", n,
	   NET_LINK_SCAN_MAX);

	/* --- strongest first ------------------------------------------ */
	net_link_scan_sort(t, n);
	eq("strongest first", find(t, n, "n05"), 0);
	int sorted = 1;

	for (int i = 1; i < n; i++) {
		if (t[i - 1].rssi < t[i].rssi) {
			sorted = 0;
		}
	}
	eq("in falling order throughout", sorted, 1);

	/* --- the rejoin backoff: 5 s doubling, capped at 5 min ---------- */
	eq("first rejoin after 5 s", (int)net_link_rejoin_delay_ms(0), 5000);
	eq("then 10 s", (int)net_link_rejoin_delay_ms(1), 10000);
	eq("then 20 s", (int)net_link_rejoin_delay_ms(2), 20000);
	eq("the sixth wait is 160 s", (int)net_link_rejoin_delay_ms(5), 160000);
	eq("the seventh is capped at 300 s", (int)net_link_rejoin_delay_ms(6),
	   300000);
	eq("then 30 min", (int)net_link_rejoin_delay_ms(7), 1800000);
	eq("and stays there for good", (int)net_link_rejoin_delay_ms(4000000000u),
	   1800000);

	/* --- a scan that never ran is not an empty air ----------------- */
	eq("an error with an empty table is the error",
	   net_link_scan_answer(-19, 0), -19);
	eq("no error is the count", net_link_scan_answer(0, 7), 7);
	eq("no error and no networks is zero", net_link_scan_answer(0, 0), 0);
	eq("a scan in progress stays -EAGAIN-shaped (negative)",
	   net_link_scan_answer(-11, 3) < 0, 1);

	return fails ? 1 : 0;
}
