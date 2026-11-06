#ifndef NET_LINK_H
#define NET_LINK_H

/*
 * The station half of the radio (spec S5.1): scan, join, and down. Free of
 * Zephyr includes, so host tests and the panel's pure code can include it.
 *
 * Every request returns at once. The work runs in net_link_work(), on Task
 * 2.5's network thread once it exists and until then on a work queue of its
 * own (net_link.c), never on the main thread: a join waits up to 30 s for
 * the router.
 *
 * An unpaired board never brings the interface up (D42). A scan brings it
 * up, and if no join follows it goes down again 60 s after the last scan.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

enum net_link_state { NL_OFF, NL_SCANNING, NL_JOINING, NL_UP, NL_FAILED };
enum net_link_why {
	NL_WHY_NONE, NL_WHY_WRONG_PSK, NL_WHY_NOT_FOUND, NL_WHY_NO_IP,
	NL_WHY_WPA3_ONLY, NL_WHY_SELFTEST, NL_WHY_FORGET
};

struct net_link_ap {
	uint8_t ssid[32];
	uint8_t ssid_len;
	int8_t rssi;
	uint8_t sec;
};

#define NL_SEC_OPEN 0
#define NL_SEC_WPA2 1
#define NL_SEC_WPA3 2
#define NL_SEC_MIXED 3

/* Spec S6.1: one wifi_net line per network, strongest first, at most 20. */
#define NET_LINK_SCAN_MAX 20

void net_link_request_scan(void);
/* -EAGAIN while scanning. */
int net_link_scan_results(struct net_link_ap *out, int max);
/* Disconnect-and-wait first if associated (esp_wifi_drv.c:495). */
void net_link_request_join(const uint8_t *ssid, size_t ssid_len, const char *psk);
/* Sockets closed, NET_REQUEST_WIFI_DISCONNECT (wait <= 2 s), net_if_down(),
 * esp_wifi_stop(): D46. */
void net_link_request_down(void);
void net_link_work(void);
enum net_link_state net_link_state(void);
enum net_link_why net_link_why(void);
const char *net_link_why_text(enum net_link_why w);
bool net_link_ip(char *buf, size_t len);
/* -127 when not joined: no reading, and below every Signal band (S5.7). */
int8_t net_link_rssi(void);

/*
 * One scan result into the table: the same network name is kept once, at its
 * strongest reading (an SSID shows up once per access point and channel),
 * and a full table gives up its weakest entry to a stronger newcomer. Pure,
 * so tests/net_link/host_test.c pins it; net_link.c calls it from the scan
 * event and sorts once the scan is done.
 */
static inline void net_link_scan_add(struct net_link_ap *tab, int *n,
				     const uint8_t *ssid, size_t len,
				     int8_t rssi, uint8_t sec)
{
	int weakest = 0;

	if (len == 0 || len > sizeof(tab[0].ssid)) {
		return;	/* a hidden network: typed by hand instead (D22) */
	}
	for (int i = 0; i < *n; i++) {
		if (tab[i].ssid_len == len && memcmp(tab[i].ssid, ssid, len) == 0) {
			if (rssi > tab[i].rssi) {
				tab[i].rssi = rssi;
				tab[i].sec = sec;
			}
			return;
		}
		if (tab[i].rssi < tab[weakest].rssi) {
			weakest = i;
		}
	}
	if (*n < NET_LINK_SCAN_MAX) {
		weakest = (*n)++;
	} else if (rssi <= tab[weakest].rssi) {
		return;
	}
	memcpy(tab[weakest].ssid, ssid, len);
	tab[weakest].ssid_len = (uint8_t)len;
	tab[weakest].rssi = rssi;
	tab[weakest].sec = sec;
}

/* Strongest first. Insertion sort: twenty entries, once a scan. */
static inline void net_link_scan_sort(struct net_link_ap *tab, int n)
{
	for (int i = 1; i < n; i++) {
		struct net_link_ap a = tab[i];
		int j = i;

		while (j > 0 && tab[j - 1].rssi < a.rssi) {
			tab[j] = tab[j - 1];
			j--;
		}
		tab[j] = a;
	}
}

#endif /* NET_LINK_H */
