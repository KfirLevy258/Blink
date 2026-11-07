/*
 * net_link.c, built unchanged, on a fake radio: the scan table behind wifi_net
 * (spec S6.1) and, since PR 9's second review, the join and rejoin scheduler.
 *
 *   tests/ci/check_host_tests.sh runs this (its row has the build line).
 *
 * The scan table: one entry per network name, at its strongest reading,
 * strongest first, at most 20. The same name arrives once per access point
 * and channel -- a mesh router is three or four of them -- and an apartment
 * block's air holds more than 20 networks, so a table that kept the first
 * twenty it heard, or each mesh node separately, would show the owner a list
 * missing their own network.
 *
 * The scheduler was covered by nothing but its delay table: net_link_work,
 * auto_rejoin and wifi_evt ran only on a board, and a regression in them
 * leaves a charger-only desk off the network until it is power-cycled, with
 * the daemon logging nothing. The file is #included, not linked, so the test
 * can read and reset its statics; the Zephyr and ESP-IDF headers are
 * tests/net_link/stub, and everything the radio does is in the fakes below.
 */
#include "net_link.c"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>


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


int64_t fake_now_ms = 1000000;

/* --- printk, captured ---------------------------------------------------- */
static char console[8192];
void printk(const char *fmt, ...)
{
	va_list ap;
	size_t l = strlen(console);

	va_start(ap, fmt);
	vsnprintf(console + l, sizeof(console) - l, fmt, ap);
	va_end(ap);
}
static int said(const char *s)
{
	int n = 0;

	for (const char *p = console; (p = strstr(p, s)) != NULL; p++) {
		n++;
	}
	return n;
}

/* --- the fake radio ------------------------------------------------------ */
static struct net_if fake_if;
struct net_mgmt_event_callback fake_cb;
struct { const char *ssid; int rssi; enum wifi_security_type sec; } f_air[4];
static int f_air_n;
static int f_n_connect, f_n_scan, f_n_disc, f_n_stop, f_n_if_up, f_n_if_down;
static int f_n_close_all;
static int f_connect_rc, f_scan_rc;
static enum { OUT_OK, OUT_DROP, OUT_SILENT } f_outcome;
static enum wifi_iface_state f_iface_st;
static size_t f_last_ssid_len;
static bool f_paired, f_pairing_ok;
struct k_heap _system_heap;

struct net_if *net_if_get_first_wifi(void) { return &fake_if; }
void net_if_up(struct net_if *i) { i->admin_up = 1; f_n_if_up++; }
void net_if_down(struct net_if *i) { i->admin_up = 0; f_n_if_down++; }
bool net_if_is_admin_up(struct net_if *i) { return i->admin_up; }
struct in_addr *net_if_ipv4_get_global_addr(struct net_if *i, int st)
{
	(void)i; (void)st;
	return NULL;
}
const char *net_addr_ntop(int af, const void *src, char *dst, size_t n)
{
	(void)af; (void)src; (void)dst; (void)n;
	return NULL;
}
int esp_wifi_stop(void) { f_n_stop++; return 0; }
int sys_heap_runtime_stats_get(struct sys_heap *h, struct sys_memory_stats *st)
{
	(void)h;
	st->free_bytes = 1000;
	return 0;
}
void wl_server_close_all(void) { f_n_close_all++; }
bool cfg_paired(void) { return f_paired; }
bool cfg_get_pairing(uint8_t ssid[32], size_t *sl, char *psk, size_t pl,
		     uint8_t key[CFG_PAIR_KEY_LEN])
{
	if (!f_pairing_ok) {
		return false;
	}
	memcpy(ssid, "home", 4);
	*sl = 4;
	snprintf(psk, pl, "passw0rd");
	memset(key, 7, CFG_PAIR_KEY_LEN);
	return true;
}

static void ev(uint64_t what, const void *info)
{
	fake_cb.info = info;
	wifi_evt(&fake_cb, what, &fake_if);
}
static void ev_connect(int status)
{
	struct wifi_status st = { .status = status };

	ev(NET_EVENT_WIFI_CONNECT_RESULT, &st);
}

int net_mgmt(uint64_t req, struct net_if *iface, void *buf, size_t len)
{
	(void)iface; (void)len;
	if (req == NET_REQUEST_WIFI_IFACE_STATUS) {
		struct wifi_iface_status *st = buf;

		st->state = f_iface_st;
		st->rssi = -50;
		return 0;
	}
	if (req == NET_REQUEST_WIFI_DISCONNECT) {
		f_n_disc++;
		f_iface_st = WIFI_STATE_DISCONNECTED;
		ev(NET_EVENT_WIFI_DISCONNECT_RESULT, NULL);
		return 0;
	}
	if (req == NET_REQUEST_WIFI_SCAN) {
		f_n_scan++;
		if (f_scan_rc != 0) {
			return f_scan_rc;
		}
		for (int i = 0; i < f_air_n; i++) {
			struct wifi_scan_result r = {
				.ssid_length = (uint8_t)strlen(f_air[i].ssid),
				.rssi = (int8_t)f_air[i].rssi,
				.security = f_air[i].sec,
			};

			memcpy(r.ssid, f_air[i].ssid, r.ssid_length);
			ev(NET_EVENT_WIFI_SCAN_RESULT, &r);
		}
		ev(NET_EVENT_WIFI_SCAN_DONE, NULL);
		return 0;
	}
	if (req == NET_REQUEST_WIFI_CONNECT) {
		const struct wifi_connect_req_params *p = buf;

		f_n_connect++;
		f_last_ssid_len = p->ssid_length;
		if (f_connect_rc != 0) {
			if (f_connect_rc == -EIO) {
				ev_connect(-1);	/* esp_wifi_drv.c:525 */
			}
			return f_connect_rc;
		}
		if (f_outcome == OUT_OK) {
			f_iface_st = WIFI_STATE_COMPLETED;
			ev_connect(0);
		} else if (f_outcome == OUT_DROP) {
			ev(NET_EVENT_WIFI_DISCONNECT_RESULT, NULL);
		}
		return 0;
	}
	return -ENOTSUP;
}

/* Nothing carries over from one scenario to the next. */
static void reset(void)
{
	ops = 0;
	rejoin_want = rejoin_cancel = 0;
	rejoin_at = 0;
	rejoin_n = 0;
	rssi_at = 0;
	boot_checked = false;
	scanning = 0;
	run_ticket = 0;
	req_pending = false;
	req_ssid_len = 0;
	state = NL_OFF;
	why = NL_WHY_NONE;
	refused = false;
	conn_status = 0;
	joining = false;
	dropped = false;
	idle_at = 0;
	scan_n = 0;
	scan_err = 0;
	f_air_n = 0;
	f_n_connect = f_n_scan = f_n_disc = f_n_stop = 0;
	f_n_if_up = f_n_if_down = f_n_close_all = 0;
	f_connect_rc = f_scan_rc = 0;
	f_outcome = OUT_OK;
	f_iface_st = WIFI_STATE_DISCONNECTED;
	f_paired = true;
	f_pairing_ok = true;
	console[0] = 0;
	fake_if.admin_up = 1;	/* Zephyr brings it up at boot */
	fake_now_ms += 1000000;
	net_link_init();	/* takes it down, as at boot (D42) */
	f_n_if_down = 0;
}

static void air(const char *ssid, int rssi, enum wifi_security_type sec)
{
	f_air[f_air_n].ssid = ssid;
	f_air[f_air_n].rssi = rssi;
	f_air[f_air_n++].sec = sec;
}

/* The network thread's loop, minus the sockets: let `ms` pass, then do what
 * net_link_due() says there is to do (bounded, so a due() stuck true fails
 * the test instead of hanging it). */
static void run(int64_t ms)
{
	fake_now_ms += ms;
	for (int i = 0; i < 20 && net_link_due(); i++) {
		net_link_work();
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


/* A paired board joins at boot, loses the router, comes back. */
static void boot_joins_once(void)
{
	reset();
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	run(0);
	eq("a paired board joins at boot", state == NL_UP, 1);
	eq("with one scan and one connect", f_n_scan * 10 + f_n_connect, 11);
	eq("nothing is left scheduled", rejoin_at == 0 && rejoin_n == 0, 1);
}

static void rejoin_backoff_and_reset(void)
{
	reset();
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	f_outcome = OUT_DROP;	/* the router never takes us */
	run(0);
	eq("a refused boot join fails", state == NL_FAILED &&
	   why == NL_WHY_WRONG_PSK, 1);
	eq("and the next try is 5 s off", rejoin_at - fake_now_ms, 5000);
	eq("counting from one", (int)rejoin_n, 1);
	run(4999);
	eq("nothing sooner than that", f_n_connect, 1);
	run(1);
	eq("then the second try", f_n_connect, 2);
	eq("waits 10 s", rejoin_at - fake_now_ms, 10000);
	f_outcome = OUT_OK;
	run(10000);
	eq("a later try joins", state == NL_UP, 1);
	eq("the count starts over after a success", (int)rejoin_n, 0);
	eq("and nothing is scheduled", rejoin_at, 0);
	run(2000000);
	eq("and no more attempts while up", f_n_connect, 3);
}

static void lost_network_waits_then_rejoins(void)
{
	reset();
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	run(0);
	eq("up", state == NL_UP, 1);
	f_iface_st = WIFI_STATE_DISCONNECTED;
	ev(NET_EVENT_WIFI_DISCONNECT_RESULT, NULL);
	eq("the router's disconnect turns the state off", state == NL_OFF, 1);
	eq("and is said", said("[wifi] lost the network"), 1);
	run(0);
	eq("the radio is stopped for it", f_n_stop, 1);
	eq("the first rejoin waits 5 s: attempt one, not zero",
	   rejoin_at - fake_now_ms == 5000 && rejoin_n == 1, 1);
	run(4999);
	eq("not a moment sooner", f_n_connect, 1);
	f_outcome = OUT_DROP;
	run(1);
	eq("then it tries", f_n_connect, 2);
	eq("and a failed one waits the second step, 10 s",
	   rejoin_at - fake_now_ms, 10000);
}

static void a_requested_join_beats_a_rejoin(void)
{
	uint8_t other[5] = "other";
	uint32_t t;

	/* In the same wake-up as the lost network. */
	reset();
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	run(0);
	f_iface_st = WIFI_STATE_DISCONNECTED;
	ev(NET_EVENT_WIFI_DISCONNECT_RESULT, NULL);
	t = net_link_request_join(other, 5, "passw0rd");
	f_outcome = OUT_DROP;
	run(0);
	eq("the requested join ran", t != 0 && net_link_run_ticket() == t &&
	   f_last_ssid_len == 5, 1);
	eq("the lost-network rejoin was not armed over it", rejoin_at, 0);
	eq("its failure shows", state == NL_FAILED, 1);
	f_n_connect = 0;
	run(600000);
	eq("and nothing later overwrites it with the stored network",
	   f_n_connect, 0);

	/* After the rejoin was already waiting. */
	reset();
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	run(0);
	f_iface_st = WIFI_STATE_DISCONNECTED;
	ev(NET_EVENT_WIFI_DISCONNECT_RESULT, NULL);
	run(0);
	eq("a rejoin is waiting", rejoin_at != 0, 1);
	t = net_link_request_join(other, 5, "passw0rd");
	f_outcome = OUT_DROP;
	run(1000);
	eq("the request ran", net_link_run_ticket() == t, 1);
	eq("and cancelled the wait", rejoin_at, 0);
	f_n_connect = 0;
	run(600000);
	eq("no stored-network join later", f_n_connect, 0);
}

static void down_and_forget_cancel_a_rejoin(void)
{
	reset();
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	run(0);
	f_iface_st = WIFI_STATE_DISCONNECTED;
	ev(NET_EVENT_WIFI_DISCONNECT_RESULT, NULL);
	run(0);
	eq("a rejoin is waiting", rejoin_at != 0, 1);
	net_link_request_down();	/* what Forget queues (proto_wifi_cancel) */
	run(0);
	eq("a down cancels it", rejoin_at, 0);
	f_n_connect = 0;
	run(2000000);
	eq("and the board stays off", f_n_connect == 0 && state == NL_OFF, 1);
}

static void a_refused_board_never_joins(void)
{
	uint8_t n[4] = "home";
	struct net_link_ap out[2];

	reset();
	net_link_refuse_selftest();
	run(0);
	eq("a refused board does not make the boot join",
	   f_n_connect + f_n_scan + f_n_if_up, 0);
	eq("and reads FAILED with the self-test reason",
	   state == NL_FAILED && why == NL_WHY_SELFTEST, 1);
	eq("and schedules no rejoin either", rejoin_at, 0);
	eq("a join request is refused", (int)net_link_request_join(n, 4, "passw0rd"), 0);
	net_link_request_scan();
	eq("a scan request reads as a failure, not an empty air",
	   net_link_scan_results(out, 2) < 0 &&
	   net_link_scan_results(out, 2) != -EAGAIN, 1);
	net_link_request_down();
	net_link_request_rejoin();
	run(0);
	eq("a down does not turn the failure into OFF", state == NL_FAILED, 1);
	/* Defence in depth: nothing can schedule a rejoin on a refused board,
	 * but if something did, the work would still not act on it. */
	rejoin_at = fake_now_ms;
	run(0);
	eq("a due rejoin on a refused board is not run", f_n_connect, 0);
}

static void a_pairing_that_cannot_be_read_ends_the_rejoin(void)
{
	reset();
	f_pairing_ok = false;	/* forgotten meanwhile */
	run(0);
	eq("no join without a readable pairing", f_n_connect, 0);
	eq("and the rejoin is dropped, not retried at every wake-up",
	   rejoin_at, 0);
}

static void a_scan_keeps_the_radio_up_for_60_s(void)
{
	reset();
	f_paired = false;	/* a scan is all an unpaired board does */
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	net_link_request_scan();
	run(0);
	eq("a scan brings the radio up", f_n_if_up, 1);
	eq("and finds the network", scan_n, 1);
	eq("the state goes back to off", state == NL_OFF, 1);
	eq("with the idle down 60 s after", idle_at - fake_now_ms, 60000);
	fake_now_ms += 59999;
	net_link_work();
	eq("it is not down at 59.999 s", f_n_stop, 0);
	run(1);
	eq("it is down at 60 s", f_n_stop == 1 && f_n_if_down == 1, 1);
	eq("and nothing is left to do", net_link_due(), 0);

	/* A scan while joined must not start the idle clock. */
	reset();
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	run(0);
	net_link_request_scan();
	run(0);
	eq("a scan while up leaves it up", state == NL_UP && idle_at == 0, 1);

	/* A failed join shows its reason through the idle down. */
	reset();
	f_paired = false;
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	f_outcome = OUT_DROP;
	{
		uint8_t n[4] = "home";

		(void)net_link_request_join(n, 4, "passw0rd");
	}
	run(0);
	eq("a refused join fails", state == NL_FAILED, 1);
	run(60000);
	eq("the radio goes down after 60 s", f_n_stop, 1);
	eq("and the failure stays on show", state == NL_FAILED &&
	   why == NL_WHY_WRONG_PSK, 1);
}

static void a_radio_that_refuses_is_not_a_missing_network(void)
{
	uint8_t n[4] = "home";

	f_paired = false;
	reset();
	f_paired = false;
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	f_connect_rc = -ENOMEM;
	(void)net_link_request_join(n, 4, "passw0rd");
	run(0);
	eq("a refused request fails the join", state == NL_FAILED, 1);
	eq("with its own reason, not Network not found", why == NL_WHY_RADIO, 1);
	eq("the errno is said", said("[wifi] connect refused: -12"), 1);
	eq("once", said("connect refused"), 1);
	eq("with the join failure", said("join failed: Could not start WiFi"), 1);
	eq("never Network not found", said("Network not found"), 0);
	eq("and not retried", f_n_connect, 1);

	reset();
	f_paired = false;
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	f_connect_rc = -EIO;	/* the station never started */
	(void)net_link_request_join(n, 4, "passw0rd");
	run(0);
	eq("-EIO is retried ten times", f_n_connect, 10);
	eq("then reads as the radio's refusal", why == NL_WHY_RADIO &&
	   said("[wifi] connect refused: -5") == 1, 1);

	/* The ordinary reasons stay what they were. */
	reset();
	f_paired = false;
	f_outcome = OUT_DROP;
	(void)net_link_request_join(n, 4, "passw0rd");
	run(0);
	eq("a network never seen is Network not found",
	   why == NL_WHY_NOT_FOUND, 1);
	reset();
	f_paired = false;
	air("home", -50, WIFI_SECURITY_TYPE_PSK);
	f_outcome = OUT_DROP;
	(void)net_link_request_join(n, 4, "passw0rd");
	run(0);
	eq("one seen and refused is the wrong password",
	   why == NL_WHY_WRONG_PSK && said("connect refused") == 0, 1);
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

	boot_joins_once();
	rejoin_backoff_and_reset();
	lost_network_waits_then_rejoins();
	a_requested_join_beats_a_rejoin();
	down_and_forget_cancel_a_rejoin();
	a_refused_board_never_joins();
	a_pairing_that_cannot_be_read_ends_the_rejoin();
	a_scan_keeps_the_radio_up_for_60_s();
	a_radio_that_refuses_is_not_a_missing_network();

	printf("%d failure(s)\n", fails);
	return fails ? 1 : 0;
}
