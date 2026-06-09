#ifndef NET_WIFI_H
#define NET_WIFI_H

#include <stdbool.h>

/*
 * Connect to WiFi using the credentials in secrets.h and block until a DHCPv4
 * lease is obtained or the timeout elapses.
 * Returns 0 on success (IP acquired), negative errno otherwise.
 */
int net_wifi_connect_blocking(int timeout_seconds);

/* True if the WiFi interface currently has a global IPv4 address. */
bool net_wifi_has_ip(void);

#endif /* NET_WIFI_H */
