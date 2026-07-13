/*
 * WiFi: station mode for normal operation, SoftAP for provisioning.
 *
 * Note CONFIG_NET_CONFIG_AUTO_INIT is deliberately off: it blocks at boot until
 * the interface acquires an IPv4 address, which cannot happen before the user
 * has given us credentials -- it silently deadlocks main(). We drive the
 * interface ourselves from here.
 */
#include <zephyr/kernel.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/wifi_mgmt.h>
#include <zephyr/net/net_ip.h>
#include <zephyr/sys/printk.h>
#include <string.h>

#include "net_wifi.h"

static struct net_mgmt_event_callback wifi_cb;
static struct net_mgmt_event_callback ipv4_cb;

static K_SEM_DEFINE(sem_connected, 0, 1);
static K_SEM_DEFINE(sem_got_ip, 0, 1);
static K_SEM_DEFINE(sem_scan_done, 0, 1);

static bool have_ip;
static int connect_status;

/* Scan results are collected by the event callback into this table. */
static char (*scan_out)[33];
static int scan_max;
static int scan_count;

static void wifi_evt(struct net_mgmt_event_callback *cb, uint64_t mgmt_event,
		     struct net_if *iface)
{
	ARG_UNUSED(iface);

	switch (mgmt_event) {
	case NET_EVENT_WIFI_CONNECT_RESULT: {
		const struct wifi_status *st = (const struct wifi_status *)cb->info;

		connect_status = st->status;
		k_sem_give(&sem_connected);
		break;
	}
	case NET_EVENT_WIFI_DISCONNECT_RESULT:
		have_ip = false;
		printk("[wifi] disconnected\n");
		break;
	case NET_EVENT_WIFI_SCAN_RESULT: {
		const struct wifi_scan_result *r =
			(const struct wifi_scan_result *)cb->info;

		if (scan_out && scan_count < scan_max && r->ssid_length > 0) {
			/* Skip duplicates: the same AP shows up per-band/per-channel. */
			for (int i = 0; i < scan_count; i++) {
				if (strncmp(scan_out[i], (const char *)r->ssid,
					    r->ssid_length) == 0) {
					return;
				}
			}
			size_t n = MIN(r->ssid_length, (uint8_t)32);

			memcpy(scan_out[scan_count], r->ssid, n);
			scan_out[scan_count][n] = '\0';
			scan_count++;
		}
		break;
	}
	case NET_EVENT_WIFI_SCAN_DONE:
		k_sem_give(&sem_scan_done);
		break;
	default:
		break;
	}
}

static void ipv4_evt(struct net_mgmt_event_callback *cb, uint64_t mgmt_event,
		     struct net_if *iface)
{
	ARG_UNUSED(cb);
	ARG_UNUSED(iface);

	if (mgmt_event == NET_EVENT_IPV4_ADDR_ADD) {
		have_ip = true;
		k_sem_give(&sem_got_ip);
	}
}

int net_wifi_init(void)
{
	net_mgmt_init_event_callback(&wifi_cb, wifi_evt,
				     NET_EVENT_WIFI_CONNECT_RESULT |
				     NET_EVENT_WIFI_DISCONNECT_RESULT |
				     NET_EVENT_WIFI_SCAN_RESULT |
				     NET_EVENT_WIFI_SCAN_DONE);
	net_mgmt_add_event_callback(&wifi_cb);

	net_mgmt_init_event_callback(&ipv4_cb, ipv4_evt, NET_EVENT_IPV4_ADDR_ADD);
	net_mgmt_add_event_callback(&ipv4_cb);
	return 0;
}

int net_wifi_connect(const char *ssid, const char *psk, int timeout_s)
{
	struct net_if *iface = net_if_get_first_wifi();

	if (!iface) {
		return -ENODEV;
	}

	struct wifi_connect_req_params p = {0};

	p.ssid = (const uint8_t *)ssid;
	p.ssid_length = strlen(ssid);
	p.channel = WIFI_CHANNEL_ANY;
	p.security = (psk && psk[0]) ? WIFI_SECURITY_TYPE_PSK
				     : WIFI_SECURITY_TYPE_NONE;
	if (psk && psk[0]) {
		p.psk = (const uint8_t *)psk;
		p.psk_length = strlen(psk);
	}
	p.mfp = WIFI_MFP_OPTIONAL;

	k_sem_reset(&sem_connected);
	k_sem_reset(&sem_got_ip);
	have_ip = false;

	printk("[wifi] connecting to \"%s\"\n", ssid);
	int rc = net_mgmt(NET_REQUEST_WIFI_CONNECT, iface, &p, sizeof(p));

	if (rc) {
		printk("[wifi] connect request failed: %d\n", rc);
		return rc;
	}

	if (k_sem_take(&sem_connected, K_SECONDS(timeout_s)) != 0) {
		printk("[wifi] connect timed out\n");
		return -ETIMEDOUT;
	}
	if (connect_status != 0) {
		printk("[wifi] association failed (status %d)\n", connect_status);
		return -ECONNREFUSED;
	}

	/* Associated is not the same as usable: we need DHCP to land before any
	 * socket will work.
	 */
	net_dhcpv4_start(iface);
	if (k_sem_take(&sem_got_ip, K_SECONDS(timeout_s)) != 0) {
		printk("[wifi] no DHCP address\n");
		return -ETIMEDOUT;
	}

	printk("[wifi] connected, got an IP\n");
	return 0;
}

int net_wifi_start_ap(void)
{
	struct net_if *iface = net_if_get_wifi_sap();

	if (!iface) {
		return -ENODEV;
	}

	struct wifi_connect_req_params p = {0};

	p.ssid = (const uint8_t *)SETUP_AP_SSID;
	p.ssid_length = strlen(SETUP_AP_SSID);
	p.channel = 6;
	p.security = WIFI_SECURITY_TYPE_NONE;	/* open: the user has nothing to type */
	p.mfp = WIFI_MFP_DISABLE;

	printk("[wifi] starting AP \"%s\"\n", SETUP_AP_SSID);
	int rc = net_mgmt(NET_REQUEST_WIFI_AP_ENABLE, iface, &p, sizeof(p));

	if (rc) {
		printk("[wifi] AP enable failed: %d\n", rc);
	}
	return rc;
}

int net_wifi_stop_ap(void)
{
	struct net_if *iface = net_if_get_wifi_sap();

	if (!iface) {
		return -ENODEV;
	}
	return net_mgmt(NET_REQUEST_WIFI_AP_DISABLE, iface, NULL, 0);
}

bool net_wifi_has_ip(void)
{
	return have_ip;
}

int net_wifi_scan(char out[][33], int max, int timeout_s)
{
	struct net_if *iface = net_if_get_first_wifi();

	if (!iface) {
		return -ENODEV;
	}

	scan_out = out;
	scan_max = max;
	scan_count = 0;
	k_sem_reset(&sem_scan_done);

	int rc = net_mgmt(NET_REQUEST_WIFI_SCAN, iface, NULL, 0);

	if (rc) {
		scan_out = NULL;
		return rc;
	}
	k_sem_take(&sem_scan_done, K_SECONDS(timeout_s));
	scan_out = NULL;
	printk("[wifi] scan found %d networks\n", scan_count);
	return scan_count;
}
