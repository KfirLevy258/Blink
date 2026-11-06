/*
 * The station half of the radio (spec S5.1), mined from the deleted
 * net_wifi.c (git show 1e8abe5^:firmware/src/net_wifi.c): the net_mgmt
 * events for connect, disconnect, scan result and scan done, and the
 * security type chosen from the scan. The SoftAP, its DHCP server, the DNS
 * hijack and everything that fetched usage over the air stay deleted.
 *
 * Dormant means the interface, not the driver (D42): with CONFIG_WIFI=y the
 * device initialiser runs esp_wifi_init() at boot on every board
 * (esp_wifi_drv.c:933), paired or not. What waits is net_if_up(), and with it
 * the RF, DHCP and the sockets. Zephyr brings every interface admin-up at
 * boot (net_if_post_init), so net_link_init() takes this one down again
 * before anything can start the RF, and an unpaired board never brings it
 * back up.
 *
 * Down is four steps (D46), because net_if_down() reaches a driver's stop
 * callback through the Ethernet L2 (ethernet.c:830) and the ESP32 driver
 * registers none (esp_wifi_drv.c:971): net_if_down() alone left the RF
 * running and the association in place. The driver itself calls
 * esp_wifi_stop() only on its AP-disable path, so stopping is ours, and it
 * is the one ESP-IDF call in this file. A later scan or join restarts the
 * RF through the driver's own esp_wifi_start() (esp_wifi_drv.c:517, :643).
 * Phase 0 proved esp_wifi_stop() returns ESP_OK and that a join after it
 * succeeds (docs/research/2026-11-wifi-ram-spike.md, "Down and up").
 *
 * The console lines are spec S5.8's fixed strings and nothing else: no SSID,
 * address, RSSI or password at any level, on any path (CLAUDE.md).
 */
#include <zephyr/kernel.h>
#include <zephyr/init.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_ip.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/net_event.h>
#include <zephyr/net/wifi_mgmt.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/printk.h>
#include <mbedtls/platform_util.h>
#include <esp_wifi.h>
#include <errno.h>
#include <string.h>

#include "net_link.h"

/* A connect result of 0 is our "joined": with this option the driver raises
 * it at DHCP_BOUND, not at association (esp_wifi_drv.c:83, :305), so it means
 * an address is in hand. Without it, "joined" would be printed before the
 * router had answered. */
BUILD_ASSERT(IS_ENABLED(CONFIG_ESP32_WIFI_STA_AUTO_DHCPV4),
	     "net_link reads CONNECT_RESULT as an address bound");

/* Association plus DHCP. A wrong password ends sooner, on the driver's
 * disconnect event (the 4-way handshake times out in a few seconds); a
 * missing network ends when the station's own scan comes back empty. */
#define JOIN_WAIT_S 30
#define SCAN_WAIT_S 15
#define DISC_WAIT_S 2	/* D46's bound on the disconnect event */
#define IDLE_DOWN_MS (60 * 1000)	/* S5.1: up 60 s after the last scan */
/* esp32_wifi_connect answers -EIO until the station has STARTED, an event
 * that lands a moment after esp_wifi_start() (esp_wifi_drv.c:523); the old
 * net_wifi.c retried for the same reason. */
#define CONNECT_TRIES 10
#define CONNECT_RETRY_MS 300

#define RSSI_EVERY_S 10
#define OP_DOWN BIT(0)
#define OP_JOIN BIT(1)
#define OP_SCAN BIT(2)

static struct net_if *iface;
static struct net_mgmt_event_callback wifi_cb;
static K_SEM_DEFINE(sem_conn, 0, 1);
static K_SEM_DEFINE(sem_disc, 0, 1);
static K_SEM_DEFINE(sem_scan, 0, 1);

static atomic_t ops;
static atomic_t rssi_cache = ATOMIC_INIT(-127);
static void kick(void);
static atomic_t scanning;
static atomic_t run_ticket;
static uint32_t req_seq;	/* under lock */
static uint32_t req_ticket;	/* under lock */
static bool req_pending;	/* under lock: a request not yet taken by do_join */
static volatile enum net_link_state state = NL_OFF;
static volatile enum net_link_why why = NL_WHY_NONE;
/* Set once at boot by net_link_refuse_selftest(), never cleared: a board
 * whose cipher failed its known-answer test keeps the radio off until it
 * reboots (spec S5.4, D47). */
static volatile bool refused;
static volatile int conn_status;
static volatile bool joining;
static volatile bool dropped;
static int64_t idle_at;		/* 0: no scan is keeping the interface up */

/* The scan table is written by the net_mgmt thread and read by any caller;
 * the join request is written by a caller and read by the work. */
static struct k_spinlock lock;
static struct net_link_ap scan_tab[NET_LINK_SCAN_MAX];
static int scan_n;
static uint8_t req_ssid[32];
static size_t req_ssid_len;
static char req_psk[65];

/* The driver's scan security (esp_wifi_drv.c:255) as S6.1's four kinds, or
 * -1 for a network nothing here can join: WEP, WAPI and 802.1X (D16). The
 * driver's default branch (esp_wifi_drv.c:277) reports everything else as
 * UNKNOWN: WPA/WPA2, WPA2/WPA3 transition, OWE and WPA3-Enterprise-192
 * alike. The first two take a WPA2 passphrase and are most of what a home
 * router broadcasts, so UNKNOWN is "mixed"; an OWE or WPA3-Enterprise
 * network listed that way fails its join as a wrong password. */
static int sec_of(enum wifi_security_type s)
{
	switch (s) {
	case WIFI_SECURITY_TYPE_NONE:
		return NL_SEC_OPEN;
	case WIFI_SECURITY_TYPE_PSK:
	case WIFI_SECURITY_TYPE_WPA_PSK:
		return NL_SEC_WPA2;
	case WIFI_SECURITY_TYPE_SAE:
		return NL_SEC_WPA3;
	case WIFI_SECURITY_TYPE_UNKNOWN:
		return NL_SEC_MIXED;
	default:
		return -1;
	}
}

static void wifi_evt(struct net_mgmt_event_callback *cb, uint64_t ev,
		     struct net_if *ifc)
{
	ARG_UNUSED(ifc);

	if (ev == NET_EVENT_WIFI_CONNECT_RESULT) {
		conn_status = ((const struct wifi_status *)cb->info)->status;
		k_sem_give(&sem_conn);
	} else if (ev == NET_EVENT_WIFI_DISCONNECT_RESULT) {
		k_sem_give(&sem_disc);
		/* This driver drops the ESP reason code and does not retry
		 * (no ESP32_WIFI_STA_RECONNECT), so the one disconnect event
		 * in a join is its verdict: fail the wait at once rather than
		 * sit out the timeout, as the old net_wifi.c did. */
		if (joining) {
			dropped = true;
			k_sem_give(&sem_conn);
		} else if (state == NL_UP) {
			/* Our own down and our disconnect-first move the state
			 * off NL_UP before they disconnect, so this is the
			 * router's doing. */
			state = NL_OFF;
			printk("[wifi] lost the network\n");
			/* Without this the interface stayed admin-up with the
			 * RF on while net_link_state() said off. The rejoin,
			 * with its backoff, is Task 2.5's (cfg_get_pairing). */
			atomic_or(&ops, OP_DOWN);
			kick();
		}
	} else if (ev == NET_EVENT_WIFI_SCAN_RESULT) {
		const struct wifi_scan_result *r = cb->info;
		int sec = sec_of(r->security);

		if (sec >= 0) {
			k_spinlock_key_t key = k_spin_lock(&lock);

			net_link_scan_add(scan_tab, &scan_n, r->ssid,
					  r->ssid_length, r->rssi, (uint8_t)sec);
			k_spin_unlock(&lock, key);
		}
	} else if (ev == NET_EVENT_WIFI_SCAN_DONE) {
		k_sem_give(&sem_scan);
	}
}

/* --- the work --------------------------------------------------------- */

static void radio_up(void)
{
	if (!net_if_is_admin_up(iface)) {
		net_if_up(iface);
		printk("[wifi] radio up\n");
	}
}

/* Connecting or connected, in the driver's terms (esp_wifi_drv.c:763):
 * the two states in which esp32_wifi_connect answers -EALREADY. */
static bool iface_state(enum wifi_iface_state *out)
{
	struct wifi_iface_status st = { 0 };

	if (net_mgmt(NET_REQUEST_WIFI_IFACE_STATUS, iface, &st, sizeof(st)) != 0) {
		return false;
	}
	*out = st.state;
	return true;
}

static bool associated(void)
{
	enum wifi_iface_state s;

	return iface_state(&s) && s >= WIFI_STATE_SCANNING &&
	       s <= WIFI_STATE_COMPLETED;
}

static void disconnect_wait(void)
{
	if (!net_if_is_admin_up(iface) || !associated()) {
		return;
	}
	k_sem_reset(&sem_disc);
	if (net_mgmt(NET_REQUEST_WIFI_DISCONNECT, iface, NULL, 0) == 0) {
		(void)k_sem_take(&sem_disc, K_SECONDS(DISC_WAIT_S));
	}
}

/* D46, in order. keep_failed: the idle down after a failed join leaves the
 * failure on show; a requested down is a fresh start. */
static void do_down(bool keep_failed)
{
	if (!(keep_failed && state == NL_FAILED)) {
		state = NL_OFF;
	}
	idle_at = 0;
	/* 1. Sockets: Task 2.5's network thread owns every one and closes
	 * them before it asks for down. Until it exists there are none. */
	/* 2. Disconnect, and wait for the event, bounded. */
	disconnect_wait();
	/* 3. The interface, and DHCP with it. */
	if (net_if_is_admin_up(iface)) {
		net_if_down(iface);
	}
	/* 4. The RF, which nothing above stops (see the top of this file). */
	(void)esp_wifi_stop();
}

static void scan_run(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	scan_n = 0;
	k_spin_unlock(&lock, key);
	atomic_set(&scanning, 1);
	k_sem_reset(&sem_scan);
	if (net_mgmt(NET_REQUEST_WIFI_SCAN, iface, NULL, 0) == 0) {
		(void)k_sem_take(&sem_scan, K_SECONDS(SCAN_WAIT_S));
	}
	key = k_spin_lock(&lock);
	net_link_scan_sort(scan_tab, scan_n);
	k_spin_unlock(&lock, key);
	atomic_set(&scanning, 0);
}

static void do_scan(void)
{
	enum net_link_state prev = state;

	if (prev != NL_UP && prev != NL_JOINING) {
		state = NL_SCANNING;
	}
	radio_up();
	scan_run();
	if (state == NL_SCANNING) {
		state = prev;
	}
	if (state != NL_UP) {
		idle_at = k_uptime_get() + IDLE_DOWN_MS;
	}
}

/* The network's kind from the last scan, or -1 if it was not in it. */
static int scanned_sec(const uint8_t *ssid, size_t len)
{
	int sec = -1;
	k_spinlock_key_t key = k_spin_lock(&lock);

	for (int i = 0; i < scan_n; i++) {
		if (scan_tab[i].ssid_len == len &&
		    memcmp(scan_tab[i].ssid, ssid, len) == 0) {
			sec = scan_tab[i].sec;
			break;
		}
	}
	k_spin_unlock(&lock, key);
	return sec;
}

static void say_failed(enum net_link_why w)
{
	why = w;
	printk("[wifi] join failed: %s\n", net_link_why_text(w));
}

/* The radio stays up for the usual 60 s, then goes down showing why. */
static void join_failed(enum net_link_why w)
{
	/* The reason first: main reads FAILED and then the reason. */
	say_failed(w);
	idle_at = k_uptime_get() + IDLE_DOWN_MS;
	state = NL_FAILED;
}

static void join_run(const uint8_t *ssid, size_t ssid_len, const char *psk,
		     uint32_t ticket)
{
	struct wifi_connect_req_params p = { 0 };
	enum wifi_iface_state s;
	int sec;
	int rc = -EIO;
	bool got;

	state = NL_JOINING;
	why = NL_WHY_NONE;
	idle_at = 0;
	atomic_set(&run_ticket, (atomic_val_t)ticket);	/* after the state */
	radio_up();

	/* The security type comes from the scan: PSK on a WPA3-only network
	 * fails exactly like a wrong password (net_wifi.c learned that), and
	 * the scan is also what tells "wrong password" from "not found"
	 * below, since the driver keeps the reason code to itself. A board
	 * rejoining at boot has no scan yet, so it takes one. */
	sec = scanned_sec(ssid, ssid_len);
	if (sec < 0) {
		scan_run();
		sec = scanned_sec(ssid, ssid_len);
	}
	if (sec == NL_SEC_WPA3) {
		join_failed(NL_WHY_WPA3_ONLY);
		return;
	}
	/* Open networks are refused in v1 (D22) and the page never offers
	 * one. S5.7 has no sentence for a board asked anyway; it is not a
	 * network this board can join, so "not found". */
	if (sec == NL_SEC_OPEN) {
		join_failed(NL_WHY_NOT_FOUND);
		return;
	}

	/* A driver still connecting or connected answers -EALREADY
	 * (esp_wifi_drv.c:495): disconnect first. Re-pairing to another
	 * network needs exactly this. The state is already off NL_UP, so the
	 * event is not taken for a lost network. */
	disconnect_wait();

	/* Not in the scan (sec < 0) is a hidden network, typed by hand (D22):
	 * tried as WPA2, the kind every mixed network also accepts. */
	p.ssid = ssid;
	p.ssid_length = ssid_len;
	p.psk = (const uint8_t *)psk;
	p.psk_length = strlen(psk);
	p.security = WIFI_SECURITY_TYPE_PSK;
	p.channel = WIFI_CHANNEL_ANY;
	p.band = WIFI_FREQ_BAND_2_4_GHZ;
	p.mfp = WIFI_MFP_OPTIONAL;
	p.timeout = SYS_FOREVER_MS;

	dropped = false;
	conn_status = 0;
	joining = true;
	k_sem_reset(&sem_conn);
	for (int i = 0; i < CONNECT_TRIES; i++) {
		rc = net_mgmt(NET_REQUEST_WIFI_CONNECT, iface, &p, sizeof(p));
		if (rc != -EIO && rc != -EAGAIN) {
			break;
		}
		/* The -EIO refusal raises a -1 connect result of its own
		 * (esp_wifi_drv.c:525), delivered later by the event thread.
		 * Waiting on the semaphore instead of sleeping consumes it, so
		 * it is never read as the next attempt's verdict; an -EAGAIN,
		 * which raises nothing, just waits out the delay. A reset
		 * alone was not enough: the event can land after it. */
		(void)k_sem_take(&sem_conn, K_MSEC(CONNECT_RETRY_MS));
		k_sem_reset(&sem_conn);
	}
	got = rc == 0 && k_sem_take(&sem_conn, K_SECONDS(JOIN_WAIT_S)) == 0;
	joining = false;

	if (got && !dropped && conn_status == 0) {
		state = NL_UP;
		printk("[wifi] joined\n");
		return;
	}
	if (rc == 0 && !dropped && iface_state(&s) && s == WIFI_STATE_COMPLETED) {
		/* Associated, and the router never gave an address. */
		disconnect_wait();
		join_failed(NL_WHY_NO_IP);
	} else if (rc == 0 && sec >= 0) {
		/* The network was on the air and refused us. */
		join_failed(NL_WHY_WRONG_PSK);
	} else {
		/* Never seen, or a radio that would not take the request. */
		join_failed(NL_WHY_NOT_FOUND);
	}
}

static void do_join(void)
{
	uint8_t ssid[32];
	size_t len;
	uint32_t ticket;
	char psk[sizeof(req_psk)];
	k_spinlock_key_t key = k_spin_lock(&lock);

	/* The OP_JOIN bit is cleared before this lock is taken, so a request
	 * landing in that gap leaves the bit set for a second run with the
	 * password already wiped. Only a request nobody has taken runs. */
	if (!req_pending) {
		k_spin_unlock(&lock, key);
		return;
	}
	req_pending = false;
	len = req_ssid_len;
	ticket = req_ticket;
	memcpy(ssid, req_ssid, len);
	memcpy(psk, req_psk, sizeof(psk));
	mbedtls_platform_zeroize(req_psk, sizeof(req_psk));
	k_spin_unlock(&lock, key);

	join_run(ssid, len, psk, ticket);
	mbedtls_platform_zeroize(psk, sizeof(psk));
}

/* On the work thread, for net_link_rssi(). */
static void rssi_refresh(void)
{
	struct wifi_iface_status st = { 0 };

	if (state == NL_UP &&
	    net_mgmt(NET_REQUEST_WIFI_IFACE_STATUS, iface, &st, sizeof(st)) == 0 &&
	    st.state == WIFI_STATE_COMPLETED) {
		atomic_set(&rssi_cache, st.rssi);
	} else {
		atomic_set(&rssi_cache, -127);
	}
}

void net_link_work(void)
{
	atomic_val_t o;

	if (iface == NULL) {
		return;
	}
	o = atomic_clear(&ops);
	if (o & OP_DOWN) {
		do_down(false);
	}
	if (o & OP_JOIN) {
		do_join();
	}
	if (o & OP_SCAN) {
		do_scan();
	}
	if (idle_at != 0 && k_uptime_get() >= idle_at &&
	    state != NL_UP && state != NL_JOINING) {
		do_down(true);
	}
	rssi_refresh();
}

/* --- until Task 2.5's thread: a work queue of its own ------------------ */

/* Not the system work queue: a join blocks for up to 30 s, and the driver
 * and the net stack queue their own work there (it peaked at 804 B with the
 * radio up, spike thread table). 4096 B at priority 7 is what Task 2.5's
 * network thread will take over: below main (0) and the driver (5), above
 * the log thread (14), as the spike measured it. */
#define NL_STACK 4096
#define NL_PRIO 7
static K_THREAD_STACK_DEFINE(nl_stack, NL_STACK);
static struct k_work_q nl_q;
static struct k_work_delayable nl_dw;

static void nl_handler(struct k_work *w)
{
	ARG_UNUSED(w);
	net_link_work();
	if (state == NL_UP) {
		/* Keeps the cached RSSI fresh for the panel's Signal row. */
		(void)k_work_schedule_for_queue(&nl_q, &nl_dw, K_SECONDS(RSSI_EVERY_S));
	} else if (idle_at != 0) {
		int64_t left = idle_at - k_uptime_get();

		(void)k_work_schedule_for_queue(&nl_q, &nl_dw,
						K_MSEC(left > 0 ? left : 0));
	}
}

static void kick(void)
{
	(void)k_work_reschedule_for_queue(&nl_q, &nl_dw, K_NO_WAIT);
}

static int net_link_init(void)
{
	const struct k_work_queue_config cfg = { .name = "net_link" };

	iface = net_if_get_first_wifi();
	if (iface == NULL) {
		return 0;
	}
	net_mgmt_init_event_callback(&wifi_cb, wifi_evt,
				     NET_EVENT_WIFI_CONNECT_RESULT |
				     NET_EVENT_WIFI_DISCONNECT_RESULT |
				     NET_EVENT_WIFI_SCAN_RESULT |
				     NET_EVENT_WIFI_SCAN_DONE);
	net_mgmt_add_event_callback(&wifi_cb);
	/* Zephyr brought it admin-up in net_init (POST_KERNEL); the RF itself
	 * starts only on a connect or a scan, so nothing has run yet (D42). */
	net_if_down(iface);
	k_work_init_delayable(&nl_dw, nl_handler);
	k_work_queue_start(&nl_q, nl_stack, K_THREAD_STACK_SIZEOF(nl_stack),
			   NL_PRIO, &cfg);
	return 0;
}

SYS_INIT(net_link_init, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);

/* --- the callers' half ------------------------------------------------- */

void net_link_request_scan(void)
{
	if (iface == NULL || refused) {
		return;
	}
	/* Set here, not in the work, so a caller polling straight after the
	 * request never reads the previous scan as this one's answer. */
	atomic_set(&scanning, 1);
	atomic_or(&ops, OP_SCAN);
	kick();
}

int net_link_scan_results(struct net_link_ap *out, int max)
{
	int n;
	k_spinlock_key_t key;

	if (atomic_get(&scanning)) {
		return -EAGAIN;
	}
	key = k_spin_lock(&lock);
	n = MIN(scan_n, max);
	if (n > 0) {
		memcpy(out, scan_tab, (size_t)n * sizeof(*out));
	}
	k_spin_unlock(&lock, key);
	return n;
}

uint32_t net_link_request_join(const uint8_t *ssid, size_t ssid_len, const char *psk)
{
	uint32_t ticket;
	size_t plen = strnlen(psk, sizeof(req_psk));
	k_spinlock_key_t key;

	if (iface == NULL || refused) {
		return 0;
	}
	/* The PC validated both (S6.1); this is the board validating again.
	 * WPA2's rule: 8 to 63 characters, or 64 hex digits. Refused here,
	 * the link is left as it was: a bad request does not end a good one. */
	if (ssid_len == 0 || ssid_len > sizeof(req_ssid)) {
		say_failed(NL_WHY_NOT_FOUND);
		return 0;
	}
	if (plen < 8 || plen > 64) {
		say_failed(NL_WHY_WRONG_PSK);
		return 0;
	}
	key = k_spin_lock(&lock);
	memcpy(req_ssid, ssid, ssid_len);
	req_ssid_len = ssid_len;
	memcpy(req_psk, psk, plen);
	req_psk[plen] = '\0';
	if (++req_seq == 0) {
		req_seq = 1;
	}
	ticket = req_ticket = req_seq;
	req_pending = true;
	k_spin_unlock(&lock, key);
	atomic_or(&ops, OP_JOIN);
	kick();
	return ticket;
}

void net_link_request_down(void)
{
	/* Refused too: the radio never came up, and a down would turn the
	 * NL_FAILED the Connection screen reads into NL_OFF. */
	if (iface == NULL || refused) {
		return;
	}
	/* A join still waiting would bring the radio straight back, and its
	 * password must not outlive it. */
	{
		k_spinlock_key_t key = k_spin_lock(&lock);

		req_pending = false;
		mbedtls_platform_zeroize(req_psk, sizeof(req_psk));
		mbedtls_platform_zeroize(req_ssid, sizeof(req_ssid));
		req_ssid_len = 0;
		k_spin_unlock(&lock, key);
	}
	atomic_and(&ops, ~OP_JOIN);
	atomic_or(&ops, OP_DOWN);
	kick();
}

void net_link_refuse_selftest(void)
{
	refused = true;
	why = NL_WHY_SELFTEST;
	state = NL_FAILED;
}

uint32_t net_link_run_ticket(void)
{
	return (uint32_t)atomic_get(&run_ticket);
}

enum net_link_state net_link_state(void)
{
	return state;
}

enum net_link_why net_link_why(void)
{
	return why;
}

const char *net_link_why_text(enum net_link_why w)
{
	/* S5.7's sentences, each under the panel's 47 characters. */
	switch (w) {
	case NL_WHY_WRONG_PSK:
		return "Wrong WiFi password";
	case NL_WHY_NOT_FOUND:
		return "Network not found";
	case NL_WHY_NO_IP:
		return "No address from the router";
	case NL_WHY_WPA3_ONLY:
		return "WPA3-only networks are not supported";
	case NL_WHY_SELFTEST:
		return "WiFi self-test failed";
	case NL_WHY_FORGET:
		return "Could not forget WiFi. Try again.";
	default:
		return "";
	}
}

/* For the panel only, never printed. */
bool net_link_ip(char *buf, size_t len)
{
	struct in_addr *a;

	if (iface == NULL || state != NL_UP) {
		return false;
	}
	a = net_if_ipv4_get_global_addr(iface, NET_ADDR_PREFERRED);
	return a != NULL && net_addr_ntop(AF_INET, a, buf, len) != NULL;
}

/* Main thread: the cached reading only. The status request takes ESP-IDF's
 * API lock, which a join or scan on the work thread can hold for seconds,
 * and LVGL would stall behind it. */
int8_t net_link_rssi(void)
{
	return state == NL_UP ? (int8_t)atomic_get(&rssi_cache) : -127;
}
