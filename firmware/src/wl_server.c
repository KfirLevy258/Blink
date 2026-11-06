/*
 * The network thread (spec S5.4 to S5.6), shaped by the Phase 0 spike
 * (spike/wifi-ram, spike_net.c): one thread, 4096 B at priority 7, below main
 * and the driver and above the log thread. It owns every socket and runs
 * net_link_work(), so a join that blocks for 30 s blocks only this thread,
 * and no session can be open during one (a join closes them first).
 *
 * One poll() loop: listener, live session, at most one candidate, UDP. The
 * tick is 50 ms, not the spec's 200: a reply queued by the main thread leaves
 * at the next tick, and the OTA acknowledges every 16 KB, so 200 ms each costs
 * about 16 s on a 1.3 MB image (80 acks).
 *
 * A candidate (an unauthenticated connection) never touches the live session:
 * it has its own socket, handshake state and 2 s deadline, and only an M2 that
 * verifies replaces the session (D29). A verify-only candidate (USB owns the
 * link, the window from wifi_set is open) runs the same M1/M2/M3, then must
 * send exactly one sealed {"t":"verify"} line; it is answered, closed, and
 * never becomes a session nor reaches proto.
 *
 * The rings are single-producer single-consumer. A slot carries the epoch of
 * the session it belongs to; every close and every replacement bumps the
 * epoch, and each reader drops slots of an older one, so neither thread has to
 * write the other's index to clear a ring.
 *
 * Nothing here prints a key, nonce, tag, address, length or byte
 * (CLAUDE.md): the console lines are four fixed strings.
 */
#include <zephyr/kernel.h>
#include <zephyr/net/net_ip.h>
#include <zephyr/net/socket.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/printk.h>
#include <mbedtls/platform_util.h>
#include <errno.h>
#include <string.h>

#include "cfg_store.h"
#include "link.h"
#include "net_link.h"
#include "wl_crypto.h"
#include "wl_frame.h"
#include "wl_hs.h"
#include "wl_server.h"

#define WL_PORT 49731
#define WL_TX_LINE_MAX 513	/* a 512-byte line and its newline */
#define CAND_DEADLINE_MS 2000
#define TICK_MS 50
#define VF_BODY_MAX 64		/* the verify line is 14 bytes; no more is read */
#define VF_FRAME_MAX (2 + VF_BODY_MAX + WL_TAG_LEN)
#define STACK_SIZE 4096
#define PRIO 7

struct rx_slot {
	uint32_t epoch;
	uint16_t len;			/* type + body */
	uint8_t pt[WL_PT_MAX];
};
struct tx_slot {
	uint32_t epoch;
	uint16_t len;
	char line[WL_TX_LINE_MAX];
};
static struct rx_slot rx_ring[WL_RX_FRAMES];
static struct tx_slot tx_ring[WL_TX_LINES];
static atomic_t rx_head, rx_tail, tx_head, tx_tail;
static atomic_t epoch, session_gen;
static atomic_t live;		/* a full session is open */
static atomic_t close_req, close_all_req, listen_off;

static int lsock = -1, usock = -1, csock = -1, ssock = -1;
static bool rng_dead;		/* until reboot: a repeated Nb would replay M2 */

/* The pairing key, read at each use and wiped after: a re-pair persists its
 * new key only once the join is up, so a copy cached at listen time was the
 * old one and every handshake after it was refused. */
static bool load_key(uint8_t key[CFG_PAIR_KEY_LEN])
{
	uint8_t ssid[32];
	size_t slen;
	char psk[65];
	bool ok = cfg_get_pairing(ssid, &slen, psk, sizeof(psk), key);

	mbedtls_platform_zeroize(psk, sizeof(psk));
	return ok;
}

/* The candidate. */
enum { C_NONE, C_M2, C_VFRAME };
static int c_state;
static bool c_verify;
static struct wl_hs hs;
static uint8_t m2buf[WL_M2_LEN];
static size_t c_got;
static int64_t c_deadline;
static struct wl_dir c_rx, c_tx;	/* held only while a verify frame is awaited */
static uint8_t c_frame[VF_FRAME_MAX];
static size_t c_fgot, c_fneed;

static struct wl_dir rx_dir, tx_dir;
static uint8_t s_frame[WL_FRAME_MAX];	/* the session frame being read */
static size_t s_got, s_need;
static uint8_t tbuf[WL_FRAME_MAX];	/* the frame being sent */
static int64_t last_answer, open_after;

static K_THREAD_STACK_DEFINE(wl_stack, STACK_SIZE);
static struct k_thread wl_thread;
static atomic_t started;

/* --- sockets ------------------------------------------------------------ */

static void close_fd(int *fd)
{
	if (*fd >= 0) {
		zsock_close(*fd);
		*fd = -1;
	}
}

static void cand_drop(void)
{
	close_fd(&csock);
	wl_hs_wipe(&hs);
	wl_dir_wipe(&c_rx);
	wl_dir_wipe(&c_tx);
	memset(m2buf, 0, sizeof(m2buf));
	memset(c_frame, 0, sizeof(c_frame));
	c_state = C_NONE;
	c_got = 0;
	c_fgot = 0;
}

/* Wipes both directions and bumps the epoch, so both rings read as empty. A
 * full session's end also ends the routing to it (link_wifi_up), which is
 * what link_emit's -ENOSPC path relies on to get its socket closed. */
static void session_close(void)
{
	if (ssock < 0) {
		return;
	}
	close_fd(&ssock);
	wl_dir_wipe(&rx_dir);
	wl_dir_wipe(&tx_dir);
	memset(s_frame, 0, sizeof(s_frame));
	s_got = 0;
	atomic_inc(&epoch);
	atomic_inc(&session_gen);
	atomic_clear(&live);
	link_wifi_up(false);
	atomic_set(&tx_tail, atomic_get(&tx_head));
	printk("[wifi] session closed\n");
}

void wl_server_close_all(void)
{
	session_close();
	cand_drop();
	close_fd(&lsock);
	close_fd(&usock);
}

static bool send_all(int fd, const uint8_t *p, size_t n)
{
	while (n) {
		ssize_t k = zsock_send(fd, p, n, 0);

		if (k <= 0) {
			return false;
		}
		p += k;
		n -= (size_t)k;
	}
	return true;
}

static void sock_opts(int fd)
{
	struct zsock_timeval tv = { .tv_sec = 1 };
	int one = 1;

	/* A stuck peer costs the thread one second, then the socket. */
	zsock_setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
	zsock_setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
}

static void sockets_open(void)
{
	struct sockaddr_in a = {
		.sin_family = AF_INET,
		.sin_port = htons(WL_PORT),
		.sin_addr.s_addr = htonl(INADDR_ANY),
	};

	if (rng_dead || !cfg_paired()) {
		return;
	}
	lsock = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	if (lsock < 0 || zsock_bind(lsock, (struct sockaddr *)&a, sizeof(a)) ||
	    zsock_listen(lsock, 1)) {
		close_fd(&lsock);
		return;
	}
	usock = zsock_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	if (usock < 0 || zsock_bind(usock, (struct sockaddr *)&a, sizeof(a))) {
		close_fd(&usock);	/* the TCP side still works, by address */
	}
}

/* --- the handshake ------------------------------------------------------ */

static void on_accept(void)
{
	uint8_t m1[WL_M1_LEN];
	int fd = zsock_accept(lsock, NULL, NULL);
	enum link_accept acc;

	if (fd < 0) {
		return;
	}
	acc = link_wifi_may_accept(k_uptime_get());
	/* Refused connections are accepted and closed, so a client sees the end
	 * at once rather than a hang in the backlog. */
	if (acc == LINK_ACCEPT_NO || csock >= 0 || rng_dead) {
		zsock_close(fd);
		return;
	}
	if (wl_hs_m1(&hs, m1) != 0) {
		/* A repeated nonce would let a captured M2 replay: stop
		 * listening until reboot (S5.4). */
		zsock_close(fd);
		wl_hs_wipe(&hs);	/* a failed draw may have left a partial Nb */
		printk("[wifi] selftest failed\n");
		rng_dead = true;
		close_fd(&lsock);
		close_fd(&usock);
		return;
	}
	sock_opts(fd);
	if (!send_all(fd, m1, sizeof(m1))) {
		zsock_close(fd);
		wl_hs_wipe(&hs);
		return;
	}
	csock = fd;
	c_state = C_M2;
	c_verify = acc == LINK_ACCEPT_VERIFY_ONLY;
	c_got = 0;
	c_deadline = k_uptime_get() + CAND_DEADLINE_MS;
}

/* Reads one frame into buf without ever reading past a bad header: 1 when
 * complete (*need bytes), 0 for more, -1 to close. cap is buf's size. */
static int frame_read(int fd, uint8_t *buf, size_t cap, size_t *got,
		      size_t *need)
{
	size_t want = *got < 2 ? 2 - *got : *need - *got;
	ssize_t n = zsock_recv(fd, buf + *got, want, ZSOCK_MSG_DONTWAIT);

	if (n == 0 || (n < 0 && errno != EAGAIN)) {
		return -1;
	}
	if (n < 0) {
		return 0;
	}
	*got += (size_t)n;
	if (*got == 2) {
		int len = wl_frame_len(buf);

		if (len < 0 || 2 + (size_t)len + WL_TAG_LEN > cap) {
			return -1;
		}
		*need = 2 + (size_t)len + WL_TAG_LEN;
		return 0;
	}
	return (*got >= 2 && *got >= *need) ? 1 : 0;
}

static void cand_fail(void)
{
	link_auth_failed(k_uptime_get());
	cand_drop();
}

static void promote(struct wl_dir *nrx, struct wl_dir *ntx)
{
	/* D29: a live session is replaced, and only by one that authenticated.
	 * Its socket is closed and keys zeroed before the new ones go in. */
	if (!link_replace_session(true)) {
		cand_drop();
		return;
	}
	session_close();
	rx_dir = *nrx;
	tx_dir = *ntx;
	ssock = csock;
	csock = -1;
	s_got = 0;
	atomic_inc(&epoch);
	atomic_inc(&session_gen);
	atomic_set(&live, 1);
	link_auth_ok();
	link_wifi_up(true);
	printk("[wifi] session open\n");
}

static void on_vframe(void)
{
	uint8_t type, body[VF_BODY_MAX];
	int r = frame_read(csock, c_frame, sizeof(c_frame), &c_fgot, &c_fneed);
	int n;

	if (r == 0) {
		return;
	}
	if (r < 0) {
		cand_fail();
		return;
	}
	n = wl_frame_open(&c_rx, c_frame, c_fneed, &type, body, sizeof(body));
	if (n < 0 || type != WL_T_LINE || !wl_verify_body_ok(body, (size_t)n)) {
		cand_fail();
		return;
	}
	static const char ok[] = "{\"t\":\"verified\"}\n";
	int f = wl_frame_seal(&c_tx, WL_T_LINE, (const uint8_t *)ok,
			      sizeof(ok) - 1, tbuf, sizeof(tbuf));

	if (f < 0 || !send_all(csock, tbuf, (size_t)f)) {
		cand_fail();
		return;
	}
	cand_drop();
	link_auth_ok();	/* clears the backoff and closes the window */
	printk("[wifi] verified\n");
}

static void on_candidate(void)
{
	uint8_t m3[WL_M3_LEN];
	struct wl_dir nrx = { 0 }, ntx = { 0 };
	uint8_t key[CFG_PAIR_KEY_LEN];
	ssize_t n;
	int rc;

	if (c_state == C_VFRAME) {
		on_vframe();
		return;
	}
	n = zsock_recv(csock, m2buf + c_got, sizeof(m2buf) - c_got,
		       ZSOCK_MSG_DONTWAIT);
	if (n < 0 && errno == EAGAIN) {
		return;
	}
	if (n <= 0) {
		cand_drop();
		return;
	}
	c_got += (size_t)n;
	if (c_got < sizeof(m2buf)) {
		return;
	}
	if (!load_key(key)) {
		cand_drop();
		return;
	}
	rc = wl_hs_check_m2(&hs, key, m2buf, sizeof(m2buf));
	if (rc != 0) {
		mbedtls_platform_zeroize(key, sizeof(key));
		if (rc == -1) {
			printk("[wifi] handshake refused\n");
			cand_fail();
		} else {
			cand_drop();	/* a crypto error is not the peer's */
		}
		return;
	}
	rc = wl_hs_m3(&hs, key, m3) != 0 ||
	     wl_hs_keys(&hs, key, nrx.key, ntx.key) != 0;
	mbedtls_platform_zeroize(key, sizeof(key));
	if (rc || !send_all(csock, m3, sizeof(m3))) {
		wl_dir_wipe(&nrx);
		wl_dir_wipe(&ntx);
		cand_drop();
		return;
	}
	wl_hs_wipe(&hs);
	if (c_verify) {
		c_rx = nrx;
		c_tx = ntx;
		c_state = C_VFRAME;
		c_fgot = 0;
	} else {
		promote(&nrx, &ntx);
	}
	wl_dir_wipe(&nrx);
	wl_dir_wipe(&ntx);
}

/* --- the session -------------------------------------------------------- */

static bool rx_has_room(void)
{
	return atomic_get(&rx_head) - atomic_get(&rx_tail) < WL_RX_FRAMES;
}

static void on_session(void)
{
	int r = frame_read(ssock, s_frame, sizeof(s_frame), &s_got, &s_need);
	struct rx_slot *s;
	int body;

	if (r < 0) {
		session_close();
		return;
	}
	if (r == 0) {
		return;
	}
	s = &rx_ring[(uint32_t)atomic_get(&rx_head) % WL_RX_FRAMES];
	body = wl_frame_open(&rx_dir, s_frame, s_need, &s->pt[0], s->pt + 1,
			     sizeof(s->pt) - 1);
	s_got = 0;
	if (body < 0) {
		session_close();
		return;
	}
	s->len = (uint16_t)(body + 1);
	s->epoch = (uint32_t)atomic_get(&epoch);
	atomic_inc(&rx_head);
}

static void tx_flush(void)
{
	while (atomic_get(&tx_tail) != atomic_get(&tx_head)) {
		struct tx_slot *s = &tx_ring[(uint32_t)atomic_get(&tx_tail) % WL_TX_LINES];

		if (ssock >= 0 && s->epoch == (uint32_t)atomic_get(&epoch)) {
			int n = wl_frame_seal(&tx_dir, WL_T_LINE,
					      (const uint8_t *)s->line, s->len,
					      tbuf, sizeof(tbuf));

			if (n < 0 || !send_all(ssock, tbuf, (size_t)n)) {
				session_close();
				return;
			}
		}
		atomic_inc(&tx_tail);
	}
}

static void on_query(void)
{
	/* A few datagrams a wake-up: one a tick let a flood queue up behind. */
	for (int i = 0; i < 4; i++) {
		uint8_t q[64], a[WL_A_LEN], key[CFG_PAIR_KEY_LEN];
		struct sockaddr from;
		socklen_t fl = sizeof(from);
		ssize_t n = zsock_recvfrom(usock, q, sizeof(q), ZSOCK_MSG_DONTWAIT,
					   &from, &fl);
		int64_t now = k_uptime_get();
		int r;

		if (n <= 0) {
			return;
		}
		/* USB owns the link (S5.6), and at most one answer a second. */
		if (link_active(now) == LINK_USB || now - last_answer < 1000 ||
		    !load_key(key)) {
			continue;
		}
		r = wl_disc_answer(key, q, (size_t)n, WL_PORT, a);
		mbedtls_platform_zeroize(key, sizeof(key));
		if (r == WL_A_LEN) {
			zsock_sendto(usock, a, sizeof(a), 0, &from, fl);
			last_answer = now;
		}
	}
}

#define READY (ZSOCK_POLLIN | ZSOCK_POLLHUP | ZSOCK_POLLERR)

static void poll_once(void)
{
	struct zsock_pollfd p[4];
	int k = 0, il = -1, iu = -1, ic = -1, is = -1;

	if (lsock >= 0) {
		il = k;
		p[k++] = (struct zsock_pollfd){ .fd = lsock, .events = ZSOCK_POLLIN };
	}
	if (usock >= 0) {
		iu = k;
		p[k++] = (struct zsock_pollfd){ .fd = usock, .events = ZSOCK_POLLIN };
	}
	if (csock >= 0) {
		ic = k;
		p[k++] = (struct zsock_pollfd){ .fd = csock, .events = ZSOCK_POLLIN };
	}
	/* While the frame ring is full the session is not read: TCP's own
	 * window holds the daemon back. */
	if (ssock >= 0 && rx_has_room()) {
		is = k;
		p[k++] = (struct zsock_pollfd){ .fd = ssock, .events = ZSOCK_POLLIN };
	}
	if (k == 0) {
		k_msleep(TICK_MS);
		return;
	}
	if (zsock_poll(p, k, TICK_MS) > 0) {
		if (is >= 0 && (p[is].revents & READY)) {
			on_session();
		}
		if (ic >= 0 && csock >= 0 && (p[ic].revents & READY)) {
			on_candidate();
		}
		if (il >= 0 && lsock >= 0 && (p[il].revents & ZSOCK_POLLIN)) {
			on_accept();
		}
		if (iu >= 0 && usock >= 0 && (p[iu].revents & ZSOCK_POLLIN)) {
			on_query();
		}
	}
	if (csock >= 0 && k_uptime_get() > c_deadline) {
		cand_drop();
	}
}

/* Once a tick: which sockets should exist, and does anything end the session.
 *
 * Teardown order (D46): this loop closes every socket the moment the link is
 * not NL_UP (a lost network flips the state before its down is queued), and
 * net_link's do_down and join_run call wl_server_close_all() again before the
 * radio moves, which is idempotent. */
static void tick(void)
{
	int64_t now = k_uptime_get();
	bool up = net_link_state() == NL_UP;

	if (atomic_clear(&close_all_req)) {
		wl_server_close_all();
	}
	if (atomic_clear(&close_req)) {
		session_close();
	}
	if (!up) {
		atomic_clear(&listen_off);
		wl_server_close_all();
		return;
	}
	if (atomic_get(&listen_off) || !cfg_paired()) {
		wl_server_close_all();
		return;
	}
	if (lsock < 0 && !rng_dead && now >= open_after) {
		sockets_open();
		if (lsock < 0) {
			open_after = now + 1000;	/* no 20 Hz retry loop */
		}
	}
	/* USB spoke, or link_emit gave up on a full TX ring (it called
	 * link_wifi_up(false)): the session ends here, with its socket. */
	if (ssock >= 0 && (link_wifi_must_close(now) || !link_wifi_is_up())) {
		session_close();
	}
}

static void net_main(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	for (;;) {
		if (net_link_due()) {
			net_link_work();
		}
		tick();
		poll_once();
		tx_flush();
	}
}

/* --- the main thread's half --------------------------------------------- */

int wl_server_start(void)
{
	/* D24: a board whose cipher failed never starts this thread. main.c
	 * already skips it; this is the belt for a second caller. */
	if (net_link_state() == NL_FAILED && net_link_why() == NL_WHY_SELFTEST) {
		return -EPERM;
	}
	if (!atomic_cas(&started, 0, 1)) {
		return 0;
	}
	k_thread_create(&wl_thread, wl_stack, K_THREAD_STACK_SIZEOF(wl_stack),
			net_main, NULL, NULL, NULL, PRIO, 0, K_NO_WAIT);
	k_thread_name_set(&wl_thread, "wl_net");
	return 0;
}

void wl_server_listen(bool on)
{
	atomic_set(&listen_off, on ? 0 : 1);
}

void wl_server_stop(void)
{
	atomic_set(&close_all_req, 1);
	net_link_request_down();
}

void wl_server_close_session(void)
{
	atomic_set(&close_req, 1);
}

bool wl_server_session_up(void)
{
	return atomic_get(&live) != 0;
}

int wl_server_rx_frame(uint8_t *type, uint8_t *body, size_t max)
{
	while (atomic_get(&rx_tail) != atomic_get(&rx_head)) {
		struct rx_slot *s = &rx_ring[(uint32_t)atomic_get(&rx_tail) % WL_RX_FRAMES];
		int n = -EAGAIN;

		if (s->epoch == (uint32_t)atomic_get(&epoch) && s->len >= 1) {
			size_t bl = (size_t)s->len - 1;

			if (bl > max) {
				n = -EMSGSIZE;
			} else {
				*type = s->pt[0];
				memcpy(body, s->pt + 1, bl);
				n = (int)bl;
			}
		}
		atomic_inc(&rx_tail);
		if (n != -EAGAIN) {
			return n;
		}
	}
	return -EAGAIN;
}

uint32_t wl_server_session_gen(void)
{
	return (uint32_t)atomic_get(&session_gen);
}

int wl_server_tx_line(const char *line, int wait_ms)
{
	size_t n = strlen(line);
	int waited = 0;
	struct tx_slot *s;

	if (!atomic_get(&live)) {
		return -ENOTCONN;
	}
	if (n + 1 > WL_TX_LINE_MAX) {
		return -EINVAL;
	}
	/* Full for wait_ms means the socket stopped taking data: the caller
	 * closes the session (link_emit), never drops a reply silently. */
	while (atomic_get(&tx_head) - atomic_get(&tx_tail) >= WL_TX_LINES) {
		if (waited >= wait_ms) {
			return -ENOSPC;
		}
		k_msleep(5);
		waited += 5;
	}
	s = &tx_ring[(uint32_t)atomic_get(&tx_head) % WL_TX_LINES];
	memcpy(s->line, line, n);
	s->line[n] = '\n';
	s->len = (uint16_t)(n + 1);
	s->epoch = (uint32_t)atomic_get(&epoch);
	atomic_inc(&tx_head);
	return 0;
}

/* --- O and G sinks ------------------------------------------------------ */

static wl_sink_fn sink_o, sink_g;

void wl_server_set_sink(uint8_t type, wl_sink_fn fn)
{
	if (type == WL_T_OTA) {
		sink_o = fn;
	} else if (type == WL_T_LOGO) {
		sink_g = fn;
	}
}

void wl_server_sink(uint8_t type, const uint8_t *body, size_t n)
{
	wl_sink_fn fn = type == WL_T_OTA ? sink_o : type == WL_T_LOGO ? sink_g : NULL;

	if (fn != NULL) {
		fn(body, n);
	} else {
		wl_server_close_session();
	}
}
