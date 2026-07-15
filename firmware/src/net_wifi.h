#ifndef NET_WIFI_H
#define NET_WIFI_H

#include <stdbool.h>

#define AP_IP "192.168.4.1"
#define AP_DHCP_POOL_START "192.168.4.10"

/*
 * The SSID this board advertises while being set up, e.g. "claude-usage-5ead4c".
 * Derived from the chip's MAC so that several of these devices in one room stay
 * distinguishable -- a fixed name would be ambiguous the moment you build a
 * second one. Stable across reboots.
 */
const char *net_wifi_ap_ssid(void);

/* The AP as a WIFI: QR payload, so one scan joins the network. */
const char *net_wifi_ap_qr(void);

/* Called (from the net mgmt context) when a station associates to our AP and
 * when one leaves. Lets the setup screen show "phone connected" without a
 * serial cable -- which matters because opening the USB serial port resets
 * this board, disrupting the very connection we are trying to observe.
 */
typedef void (*net_wifi_sta_cb)(int station_count);
void net_wifi_set_sta_cb(net_wifi_sta_cb cb);

int net_wifi_init(void);

/* Optional: called repeatedly (every ~50 ms) while net_wifi blocks on scans,
 * joins, DHCP, or AP bring-up. LVGL has no thread of its own here, so without
 * this every spinner on screen freezes for the duration of the wait. */
void net_wifi_set_idle_hook(void (*hook)(void));

/* Number of DHCP leases currently granted on the AP. Lets the setup screen show
 * whether the board actually handed the phone an address, vs the phone merely
 * associating -- diagnosable without a serial cable (which resets this board).
 */
int net_wifi_ap_lease_count(void);

/* Join a network as a station. Returns 0 once an IPv4 address is held, or a
 * negative errno on failure/timeout.
 */
int net_wifi_connect(const char *ssid, const char *psk, int timeout_s);

/* Human-readable reason the last connect failed (e.g. "wrong password",
 * "network not found", "no IP address"). Valid after net_wifi_connect != 0. */
const char *net_wifi_last_error(void);

/* The station's IPv4 as text (e.g. "192.168.1.23"), for the phase-2 URL the
 * phone opens. Valid after net_wifi_connect() returned 0. Returns false if
 * the station holds no address. */
bool net_wifi_sta_ip(char *buf, size_t len);

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
