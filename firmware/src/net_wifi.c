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
#include <zephyr/net/dhcpv4_server.h>
#include <zephyr/net/dhcpv4.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/sys/printk.h>
#include <stdio.h>
#include <string.h>

#include "net_wifi.h"

static char ap_ssid[33];
static char ap_qr[64];

const char *net_wifi_ap_ssid(void)
{
	if (!ap_ssid[0]) {
		uint8_t id[16];
		ssize_t n = hwinfo_get_device_id(id, sizeof(id));

		if (n >= 3) {
			snprintf(ap_ssid, sizeof(ap_ssid), "claude-usage-%02x%02x%02x",
				 id[n - 3], id[n - 2], id[n - 1]);
		} else {
			snprintf(ap_ssid, sizeof(ap_ssid), "claude-usage-setup");
		}
	}
	return ap_ssid;
}

const char *net_wifi_ap_qr(void)
{
	if (!ap_qr[0]) {
		/* Standard WIFI: payload -- phones join straight from the scan.
		 * The AP is open, so T:nopass and no password field.
		 */
		snprintf(ap_qr, sizeof(ap_qr), "WIFI:T:nopass;S:%s;;",
			 net_wifi_ap_ssid());
	}
	return ap_qr;
}

static struct net_mgmt_event_callback wifi_cb;
static struct net_mgmt_event_callback ipv4_cb;

static K_SEM_DEFINE(sem_connected, 0, 1);
static K_SEM_DEFINE(sem_got_ip, 0, 1);
static K_SEM_DEFINE(sem_scan_done, 0, 1);
static K_SEM_DEFINE(sem_ap_up, 0, 1);

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
	case NET_EVENT_WIFI_AP_ENABLE_RESULT:
		k_sem_give(&sem_ap_up);
		break;
	case NET_EVENT_WIFI_AP_STA_CONNECTED:
		printk("[wifi] a station joined the AP\n");
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
				     NET_EVENT_WIFI_SCAN_DONE |
				     NET_EVENT_WIFI_AP_ENABLE_RESULT |
				     NET_EVENT_WIFI_AP_STA_CONNECTED);
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

	const char *ssid = net_wifi_ap_ssid();

	p.ssid = (const uint8_t *)ssid;
	p.ssid_length = strlen(ssid);
	p.channel = 6;
	p.security = WIFI_SECURITY_TYPE_NONE;	/* open: the user has nothing to type */
	p.mfp = WIFI_MFP_DISABLE;

	printk("[wifi] starting AP \"%s\"\n", ssid);
	k_sem_reset(&sem_ap_up);

	int rc = net_mgmt(NET_REQUEST_WIFI_AP_ENABLE, iface, &p, sizeof(p));

	if (rc) {
		printk("[wifi] AP enable failed: %d\n", rc);
		return rc;
	}

	/*
	 * Wait for the AP to actually come up before touching its interface.
	 * Configuring the address the instant net_mgmt() returns is too early --
	 * the interface is not operational yet, net_if_ipv4_addr_add() fails, and
	 * the result is an AP a phone can associate with but which never answers
	 * DHCP: it spins on "obtaining IP address" forever.
	 */
	if (k_sem_take(&sem_ap_up, K_SECONDS(10)) != 0) {
		printk("[wifi] AP never came up\n");
		return -ETIMEDOUT;
	}

	struct in_addr addr, netmask, pool_start;

	net_addr_pton(AF_INET, AP_IP, &addr);
	net_addr_pton(AF_INET, "255.255.255.0", &netmask);
	net_addr_pton(AF_INET, AP_DHCP_POOL_START, &pool_start);

	/* The phone wants a default route even on a network with no internet;
	 * without a gateway some clients reject the lease outright.
	 */
	net_if_ipv4_set_gw(iface, &addr);

	if (net_if_ipv4_addr_add(iface, &addr, NET_ADDR_MANUAL, 0) == NULL) {
		printk("[wifi] FAILED to set AP address %s\n", AP_IP);
		return -EADDRNOTAVAIL;
	}
	net_if_ipv4_set_netmask_by_addr(iface, &addr, &netmask);

	rc = net_dhcpv4_server_start(iface, &pool_start);
	if (rc && rc != -EALREADY) {
		printk("[wifi] DHCP server FAILED: %d\n", rc);
		return rc;
	}

	/* An interface can hold an address and still be administratively down --
	 * DHCP would work (its server binds to the interface directly) while ping
	 * and TCP get no reply at all, which is exactly the symptom we hit.
	 */
	if (!net_if_is_up(iface)) {
		printk("[wifi] AP iface was DOWN; bringing it up\n");
		net_if_up(iface);
	}

	/*
	 * Make the AP the default interface, and park the idle station.
	 *
	 * Without this the board RECEIVES fine -- DHCP requests arrive, stations
	 * associate -- but its replies go nowhere: outbound packets follow the
	 * default interface, which is the station, and while provisioning the
	 * station is down with no address. The symptom is a client that gets a
	 * DHCP lease and then cannot ping the board or load the page.
	 */
	struct net_if *sta = net_if_get_wifi_sta();

	if (sta && sta != iface && net_if_is_up(sta)) {
		net_if_down(sta);
	}
	net_if_set_default(iface);

	printk("[wifi] AP up=%d oper=%d addr=%s leases from %s\n",
	       net_if_is_up(iface), net_if_oper_state(iface),
	       AP_IP, AP_DHCP_POOL_START);
	return 0;
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
