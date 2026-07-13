#ifndef NET_WIFI_H
#define NET_WIFI_H

#include <stdbool.h>

/* The SSID the board advertises while being set up. */
#define SETUP_AP_SSID "claude-usage-setup"

int net_wifi_init(void);

/* Join a network as a station. Returns 0 once an IPv4 address is held, or a
 * negative errno on failure/timeout.
 */
int net_wifi_connect(const char *ssid, const char *psk, int timeout_s);

/* Become an open access point on 192.168.4.1 so a phone can reach the setup
 * portal. Used when there are no credentials yet, or after a factory reset.
 */
int net_wifi_start_ap(void);
int net_wifi_stop_ap(void);

bool net_wifi_has_ip(void);

/* Scan for nearby networks, so the setup page can offer a list rather than
 * making the user type an SSID by hand. Results land in `out` (SSID strings).
 * Returns the count.
 */
int net_wifi_scan(char out[][33], int max, int timeout_s);

#endif /* NET_WIFI_H */
