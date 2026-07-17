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
#include <zephyr/net/net_if.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/sys/printk.h>
#include <stdio.h>
#include <string.h>

#include "net_wifi.h"

static net_wifi_sta_cb sta_cb;
static int sta_count;
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

static void (*idle_hook)(void);

void net_wifi_set_idle_hook(void (*hook)(void))
{
	idle_hook = hook;
}

/* k_sem_take in ~50 ms slices with the idle hook between them: these waits
 * sit under live screens (splash spinner, CONNECTING), and a plain blocking
 * take visibly freezes them for the whole wait (seen on hardware
 * 2026-07-14). Returns 0 when the semaphore arrived. */
static int sem_take_pumped(struct k_sem *sem, int timeout_s)
{
	int64_t deadline = k_uptime_get() + (int64_t)timeout_s * 1000;

	for (;;) {
		if (k_sem_take(sem, K_MSEC(50)) == 0) {
			return 0;
		}
		if (k_uptime_get() >= deadline) {
			return -EAGAIN;
		}
		if (idle_hook) {
			idle_hook();
		}
	}
}

/* k_msleep that keeps the screen alive the same way. */
static void idle_wait_ms(int ms)
{
	int64_t end = k_uptime_get() + ms;

	do {
		if (idle_hook) {
			idle_hook();
		}
		k_msleep(50);
	} while (k_uptime_get() < end);
}
static K_SEM_DEFINE(sem_got_ip, 0, 1);
static K_SEM_DEFINE(sem_scan_done, 0, 1);
static K_SEM_DEFINE(sem_ap_up, 0, 1);

static bool have_ip;
static const char *last_error;
static int connect_status;

/* Set while net_wifi_connect() waits for association. A disconnect event in
 * that window is the driver's only timely word on a doomed join: wrong
 * password never yields a CONNECT_RESULT until the full timeout, and this
 * driver (a) swallows the ESP reason code (it raises the event with just a
 * -1 status) and (b) does not retry without ESP32_WIFI_STA_RECONNECT, so the
 * single disconnect event IS the verdict -- fail the wait on first sight. */
static volatile bool connecting;
static volatile bool connect_dropped;

/* Scan results are collected by the event callback into this table. */
static char (*scan_out)[33];
static int scan_max;
static int scan_count;

/* Remembered per-network security, so connect can pick PSK (WPA2) vs SAE
 * (WPA3) instead of guessing -- guessing wrong looks like a wrong password. */
#define SCANNED_MAX 16
static struct {
	char ssid[33];
	enum wifi_security_type sec;
} scanned[SCANNED_MAX];
static int scanned_n;

static void wifi_evt(struct net_mgmt_event_callback *cb, uint64_t mgmt_event,
		     struct net_if *iface)
{
	ARG_UNUSED(iface);

	switch (mgmt_event) {
	case NET_EVENT_WIFI_CONNECT_RESULT: {
		const struct wifi_status *st = (const struct wifi_status *)cb->info;

		connect_status = st->status;
		if (st->status != 0) {
			/* conn_status names WHY: 2 = wrong password, 3 = no
			 * AP found, 4 = auth timeout (enum wifi_conn_status).
			 * Telling "network gone" from "password rejected"
			 * from serial is worth a printk. */
			printk("[wifi] connect failed: status %d conn %d\n",
			       st->status, st->conn_status);
		}
		k_sem_give(&sem_connected);
		break;
	}
	case NET_EVENT_WIFI_DISCONNECT_RESULT: {
		const struct wifi_status *st = (const struct wifi_status *)cb->info;

		have_ip = false;
		printk("[wifi] disconnected (reason %d)\n", st->disconn_reason);
		if (connecting) {
			connect_dropped = true;
			k_sem_give(&sem_connected);
		}
		break;
	}
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

			if (scanned_n < SCANNED_MAX) {
				memcpy(scanned[scanned_n].ssid, r->ssid, n);
				scanned[scanned_n].ssid[n] = '\0';
				scanned[scanned_n].sec = r->security;
				scanned_n++;
			}
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
		sta_count++;
		printk("[wifi] station joined AP (now %d)\n", sta_count);
		if (sta_cb) {
			sta_cb(sta_count);
		}
		break;
	case NET_EVENT_WIFI_AP_STA_DISCONNECTED:
		if (sta_count > 0) {
			sta_count--;
		}
		printk("[wifi] station left AP (now %d)\n", sta_count);
		if (sta_cb) {
			sta_cb(sta_count);
		}
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

void net_wifi_set_sta_cb(net_wifi_sta_cb cb)
{
	sta_cb = cb;
}

int net_wifi_init(void)
{
	net_mgmt_init_event_callback(&wifi_cb, wifi_evt,
				     NET_EVENT_WIFI_CONNECT_RESULT |
				     NET_EVENT_WIFI_DISCONNECT_RESULT |
				     NET_EVENT_WIFI_SCAN_RESULT |
				     NET_EVENT_WIFI_SCAN_DONE |
				     NET_EVENT_WIFI_AP_ENABLE_RESULT |
				     NET_EVENT_WIFI_AP_STA_CONNECTED |
				     NET_EVENT_WIFI_AP_STA_DISCONNECTED);
	net_mgmt_add_event_callback(&wifi_cb);

	net_mgmt_init_event_callback(&ipv4_cb, ipv4_evt, NET_EVENT_IPV4_ADDR_ADD);
	net_mgmt_add_event_callback(&ipv4_cb);
	return 0;
}

int net_wifi_connect(const char *ssid, const char *psk, int timeout_s)
{
	/* Explicitly the STATION interface, not net_if_get_first_wifi() -- with
	 * both a station and a soft-AP present, "first" is ambiguous and may hand
	 * back the AP. And the station was brought DOWN during AP provisioning,
	 * so revive it before asking it to associate.
	 */
	struct net_if *iface = net_if_get_wifi_sta();

	if (!iface) {
		iface = net_if_get_first_wifi();
	}
	if (!iface) {
		return -ENODEV;
	}
	if (!ssid || !ssid[0]) {
		printk("[wifi] connect called with empty SSID\n");
		last_error = "No network selected";
		return -EINVAL;
	}
	if (!net_if_is_up(iface)) {
		net_if_up(iface);
		k_msleep(200);
	}
	net_if_set_default(iface);

	struct wifi_connect_req_params p = {0};

	p.ssid = (const uint8_t *)ssid;
	p.ssid_length = strlen(ssid);
	p.channel = WIFI_CHANNEL_ANY;

	/* Pick the security type from the scan: the ESP32 driver only accepts
	 * NONE / PSK / SAE, and using PSK on a WPA3-SAE network fails exactly like
	 * a wrong password. Default to PSK for a typed/hidden network we didn't
	 * scan. */
	enum wifi_security_type sec = (psk && psk[0]) ? WIFI_SECURITY_TYPE_PSK
						      : WIFI_SECURITY_TYPE_NONE;

	for (int i = 0; i < scanned_n; i++) {
		if (strcmp(scanned[i].ssid, ssid) == 0) {
			if (scanned[i].sec == WIFI_SECURITY_TYPE_NONE) {
				sec = WIFI_SECURITY_TYPE_NONE;
			}
			/* SAE (WPA3) is clamped to PSK: enabling the driver's SAE
			 * path pulls in mbedTLS ECC prerequisites that conflict
			 * with our TLS config. WPA2/WPA3-mixed networks (the common
			 * case) accept PSK; pure WPA3-only would need SAE. */
			break;
		}
	}
	printk("[wifi] connecting sec=%d to \"%s\"\n", sec, ssid);

	p.security = sec;
	if (psk && psk[0]) {
		p.psk = (const uint8_t *)psk;
		p.psk_length = strlen(psk);
	}
	p.mfp = WIFI_MFP_OPTIONAL;

	k_sem_reset(&sem_connected);
	k_sem_reset(&sem_got_ip);
	have_ip = false;
	connect_dropped = false;
	connecting = true;

	printk("[wifi] connecting to \"%s\"\n", ssid);

	/*
	 * The request is rejected with -EIO/-EAGAIN until the station has finished
	 * "starting" -- an async transition that lands a moment after we bring
	 * the interface up. Retry for a few seconds rather than failing instantly.
	 */
	int rc = -EIO;

	for (int attempt = 0; attempt < 20; attempt++) {
		rc = net_mgmt(NET_REQUEST_WIFI_CONNECT, iface, &p, sizeof(p));
		if (rc == 0) {
			break;
		}
		printk("[wifi] connect request rc=%d (attempt %d), retrying\n",
		       rc, attempt);
		idle_wait_ms(300);
	}

	if (rc) {
		connecting = false;
		printk("[wifi] connect request failed: %d\n", rc);
		last_error = "The WiFi radio wasn't ready";
		return rc;
	}

	if (sem_take_pumped(&sem_connected, timeout_s) != 0) {
		connecting = false;
		printk("[wifi] connect timed out\n");
		last_error = "Network didn't respond (name/range?)";
		return -ETIMEDOUT;
	}
	connecting = false;
	if (connect_dropped) {
		/* The driver ate the ESP reason code, so wrong password and
		 * a mid-join drop are indistinguishable here; password is by
		 * far the common cause on a fresh-credentials join. */
		printk("[wifi] join dropped mid-association\n");
		last_error = "Wrong password, or weak signal";
		return -ECONNREFUSED;
	}
	if (connect_status != 0) {
		printk("[wifi] association failed (status %d)\n", connect_status);
		last_error = "Wrong password, or network refused";
		return -ECONNREFUSED;
	}

	/* Associated is not the same as usable: we need DHCP to land before any
	 * socket will work.
	 */
	net_dhcpv4_start(iface);
	if (sem_take_pumped(&sem_got_ip, timeout_s) != 0) {
		printk("[wifi] no DHCP address\n");
		last_error = "Connected but got no IP address";
		return -ETIMEDOUT;
	}

	printk("[wifi] connected, got an IP\n");
	return 0;
}

const char *net_wifi_last_error(void)
{
	return last_error ? last_error : "connect failed";
}

bool net_wifi_sta_ip(char *buf, size_t len)
{
	struct net_if *iface = net_if_get_wifi_sta();

	if (!iface) {
		iface = net_if_get_first_wifi();
	}
	if (!iface) {
		return false;
	}

	struct in_addr *a = net_if_ipv4_get_global_addr(iface, NET_ADDR_PREFERRED);

	if (!a) {
		return false;
	}
	return net_addr_ntop(AF_INET, a, buf, len) != NULL;
}

int net_wifi_start_ap(void)
{
	struct net_if *iface = net_if_get_wifi_sap();

	if (!iface) {
		return -ENODEV;
	}

	/*
	 * Park the station FIRST: on retry rounds net_wifi_connect() revived it
	 * and a timed-out join may still be mid-association -- enabling the AP
	 * around a live station transiently reinstates AP+STA, the concurrent
	 * mode this design deletes. Downing the interface also aborts any
	 * lingering association attempt.
	 */
	struct net_if *sta = net_if_get_wifi_sta();

	if (sta && sta != iface && net_if_is_up(sta)) {
		net_if_down(sta);
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
	if (sem_take_pumped(&sem_ap_up, 10) != 0) {
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
	 * Make the AP the default interface. Without this the board RECEIVES
	 * fine -- DHCP requests arrive, stations associate -- but its replies
	 * follow the default interface (the parked station) and go nowhere:
	 * the client gets a lease and then cannot load the page. The station
	 * itself was parked at the top of this function, before the AP came up.
	 */
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

static void count_lease_cb(struct net_if *iface, struct dhcpv4_addr_slot *lease,
			   void *user_data)
{
	ARG_UNUSED(iface); ARG_UNUSED(lease);
	(*(int *)user_data)++;
}

int net_wifi_ap_lease_count(void)
{
	struct net_if *ap = net_if_get_wifi_sap();
	int n = 0;

	if (!ap) {
		return 0;
	}
	net_dhcpv4_server_foreach_lease(ap, count_lease_cb, &n);
	return n;
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
	scanned_n = 0;
	k_sem_reset(&sem_scan_done);

	int rc = net_mgmt(NET_REQUEST_WIFI_SCAN, iface, NULL, 0);

	if (rc) {
		scan_out = NULL;
		return rc;
	}
	sem_take_pumped(&sem_scan_done, timeout_s);
	scan_out = NULL;
	printk("[wifi] scan found %d networks\n", scan_count);
	return scan_count;
}
