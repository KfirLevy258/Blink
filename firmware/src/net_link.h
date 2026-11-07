#ifndef NET_LINK_H
#define NET_LINK_H

/*
 * The station half of the radio (spec S5.1): scan, join, and down. Free of
 * Zephyr includes, so host tests and the panel's pure code can include it.
 *
 * Every request returns at once. The work runs in net_link_work(), on the
 * network thread (wl_server.c), never on the main thread: a join waits up to 30 s for
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
	NL_WHY_WPA3_ONLY, NL_WHY_SELFTEST, NL_WHY_FORGET, NL_WHY_SCAN, NL_WHY_RADIO
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

/* What net_link_scan_results returns: the failure if there was one, else the
 * count. Here so tests/net_link can pin that an empty table with an error is
 * never read as "no networks". */
static inline int net_link_scan_answer(int err, int n)
{
	return err < 0 ? err : n;
}

void net_link_request_scan(void);
/* -EAGAIN while scanning, another negative errno when the scan never ran
 * (distinct from 0, an empty air). */
int net_link_scan_results(struct net_link_ap *out, int max);
/* Disconnect-and-wait first if associated (esp_wifi_drv.c:497). */
/* Returns a ticket (never 0) naming THIS request, or 0 if it was refused.
 * Asking again replaces a request that has not started; the ticket of the
 * one that wins is the one net_link_run_ticket() later reports. */
uint32_t net_link_request_join(const uint8_t *ssid, size_t ssid_len, const char *psk);
/* The stored network again, as an automatic rejoin: the first try after the
 * 5 s router wait, then the capped backoff. For a failed candidate on a paired
 * board, whose fallback join would otherwise be one-shot. No-op unpaired. */
void net_link_request_rejoin(void);
/* Sockets closed, NET_REQUEST_WIFI_DISCONNECT (wait <= 2 s), net_if_down(),
 * esp_wifi_stop(): D46. */
void net_link_request_down(void);
/* main.c, when wl_crypto_selftest() failed at boot: every later scan,
 * join and down is refused, so the radio never comes up, and the state reads
 * NL_FAILED with NL_WHY_SELFTEST ("WiFi self-test failed") for the
 * Connection screen. Until reboot. */
void net_link_refuse_selftest(void);
/* Runs on wl_server's thread. net_link_due() says whether a call has work. */
bool net_link_due(void);
/* The system heap's free bytes as last measured (after each down and each
 * automatic rejoin), or SIZE_MAX before the first. For wl_heap.h's decision. */
size_t net_link_heap_free(void);
void net_link_work(void);

/*
 * The ticket of the join now running or last run, published in join_run
 * after the state reads JOINING. A caller trusts UP/FAILED as its own only
 * when this equals the ticket its request returned: a join already running
 * (the boot join, an earlier wifi_set) or a later one (a rejoin) has another
 * ticket, and crediting its result to a candidate would persist credentials
 * nobody verified (D21). The ticket travels with the parameters, under the
 * same lock, so no preemption can pair one request's ticket with another's
 * credentials.
 */
uint32_t net_link_run_ticket(void);
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

/* The wait before automatic rejoin attempt n (0-based): 5 s, doubling to
 * 300 s (5, 10, 20, 40, 80, 160, 300), then 30 min for good. A cap of 300 s
 * limits the rate, not the total: 288 attempts a day, and each connection
 * leaks about 40 B inside the WiFi blob. 30 min is 48 a day, and a router
 * back after a power cut is found within half an hour. Pure, so
 * tests/net_link/host_test.c pins it. */
static inline int64_t net_link_rejoin_delay_ms(unsigned int n)
{
	int64_t d = 5000;

	if (n >= 7) {
		return 1800000;
	}
	while (n-- > 0 && d < 300000) {
		d *= 2;
	}
	return d > 300000 ? 300000 : d;
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
