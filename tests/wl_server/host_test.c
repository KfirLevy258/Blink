/*
 * wl_server.c, built unchanged, on fake sockets and the toy cipher.
 *
 * It used to hold only the two inline helpers of wl_server.h, so nothing ran
 * any of the file's 700 lines: not that an unauthenticated connection cannot
 * evict the live session (D29), not that a verify-only candidate never
 * reaches proto (D65), not that a bad frame ends the session, not the
 * discovery rules, not the failure backoff. Those are the lines a mistake in
 * which costs the owner their link or opens it to a stranger.
 *
 * The file is #included, not linked, so the test can reach its statics
 * (tick, poll_once, tx_flush: the body of net_main without the thread).
 * link.c, wl_hs.c and wl_frame.c are the real ones; the cipher is
 * tests/support/wl_crypto_fake.c, which tests/wl_crypto_real holds to the
 * PC's bytes separately. The Zephyr headers are tests/wl_server/stub.
 *
 *   tests/ci/check_host_tests.sh runs this (its row has the build line).
 */
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "wl_crypto.h"
#include "wl_server.c"

int64_t fake_now_ms = 1000000;

/* --- printk, captured ---------------------------------------------------- */
static char console[4096];
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

/* --- fake sockets -------------------------------------------------------- */
#define NF 64
struct fsock {
	int used, kind, closed;	/* kind 1 stream, 2 dgram, 3 listener */
	uint8_t tb[2048];	/* what the peer sent, for the board to read */
	size_t tb_len, tb_off;
	uint8_t fb[2048];	/* what the board sent */
	size_t fb_len, fb_off;
	int peer_closed;
	int pend[4], npend;
	struct { uint8_t d[64]; size_t n; } q[8];
	int nq, answers;
};
static struct fsock fs[NF];
static int next_fd;
static int fail_stream_socket, fail_dgram_socket, fail_dgram_bind;

static struct fsock *F(int fd)
{
	return fd >= 3 && fd - 3 < NF && fs[fd - 3].used ? &fs[fd - 3] : NULL;
}
int zsock_socket(int af, int type, int proto)
{
	(void)af; (void)proto;
	if ((type == SOCK_STREAM && fail_stream_socket) ||
	    (type == SOCK_DGRAM && fail_dgram_socket) || next_fd >= NF) {
		errno = 23;
		return -1;
	}
	fs[next_fd].used = 1;
	fs[next_fd].kind = type == SOCK_STREAM ? 3 : 2;
	return 3 + next_fd++;
}
int zsock_bind(int fd, const struct sockaddr *a, socklen_t l)
{
	(void)a; (void)l;
	if (F(fd)->kind == 2 && fail_dgram_bind) {
		errno = 98;
		return -1;
	}
	return 0;
}
int zsock_listen(int fd, int b) { (void)fd; (void)b; return 0; }
int zsock_accept(int fd, struct sockaddr *a, socklen_t *l)
{
	struct fsock *s = F(fd);
	int r;

	(void)a; (void)l;
	if (s->npend == 0) {
		errno = EAGAIN;
		return -1;
	}
	r = s->pend[0];
	memmove(s->pend, s->pend + 1, sizeof(int) * (size_t)--s->npend);
	return r;
}
int zsock_close(int fd) { F(fd)->closed = 1; return 0; }
int zsock_setsockopt(int fd, int v, int o, const void *p, socklen_t l)
{
	(void)fd; (void)v; (void)o; (void)p; (void)l;
	return 0;
}
ssize_t zsock_send(int fd, const void *p, size_t n, int f)
{
	struct fsock *s = F(fd);

	(void)f;
	if (s->closed || s->fb_len + n > sizeof(s->fb)) {
		return -1;
	}
	memcpy(s->fb + s->fb_len, p, n);
	s->fb_len += n;
	return (ssize_t)n;
}
ssize_t zsock_recv(int fd, void *p, size_t n, int f)
{
	struct fsock *s = F(fd);
	size_t k = s->tb_len - s->tb_off;

	(void)f;
	if (k == 0) {
		if (s->peer_closed) {
			return 0;
		}
		errno = EAGAIN;
		return -1;
	}
	k = k < n ? k : n;
	memcpy(p, s->tb + s->tb_off, k);
	s->tb_off += k;
	return (ssize_t)k;
}
ssize_t zsock_recvfrom(int fd, void *p, size_t n, int f,
		       struct sockaddr *from, socklen_t *fl)
{
	struct fsock *s = F(fd);
	size_t k;

	(void)f; (void)from; (void)fl;
	if (s->nq == 0) {
		errno = EAGAIN;
		return -1;
	}
	k = s->q[0].n < n ? s->q[0].n : n;
	memcpy(p, s->q[0].d, k);
	memmove(&s->q[0], &s->q[1], sizeof(s->q[0]) * (size_t)--s->nq);
	return (ssize_t)k;
}
ssize_t zsock_sendto(int fd, const void *p, size_t n, int f,
		     const struct sockaddr *to, socklen_t l)
{
	(void)p; (void)f; (void)to; (void)l;
	F(fd)->answers++;
	return (ssize_t)n;
}
int zsock_poll(struct zsock_pollfd *p, int n, int t)
{
	int r = 0;

	(void)t;
	for (int i = 0; i < n; i++) {
		struct fsock *s = F(p[i].fd);

		p[i].revents = 0;
		if ((s->kind == 3 && s->npend) || (s->kind == 2 && s->nq) ||
		    (s->kind == 1 && (s->tb_off < s->tb_len || s->peer_closed))) {
			p[i].revents = ZSOCK_POLLIN;
			r++;
		}
	}
	return r;
}

/* --- the board's other neighbours --------------------------------------- */
static bool paired = true;
static enum net_link_state nl_state = NL_UP;
static const uint8_t KEY[32] = "0123456789abcdef0123456789abcdef";
bool cfg_paired(void) { return paired; }
bool cfg_get_pairing(uint8_t ssid[32], size_t *sl, char *psk, size_t pl,
		     uint8_t key[CFG_PAIR_KEY_LEN])
{
	(void)ssid; (void)psk; (void)pl;
	*sl = 0;
	memcpy(key, KEY, 32);
	return paired;
}
enum net_link_state net_link_state(void) { return nl_state; }
enum net_link_why net_link_why(void) { return NL_WHY_NONE; }
bool net_link_due(void) { return false; }
void net_link_work(void) {}

/* --- the PC's half ------------------------------------------------------- */
static int cur_listener(void)
{
	for (int i = 0; i < NF; i++) {
		if (fs[i].used && fs[i].kind == 3 && !fs[i].closed &&
		    i + 3 == lsock) {
			return i;
		}
	}
	return -1;
}
/* A connect: a new stream waiting on the listener. */
static int cli_connect(void)
{
	int l = cur_listener();
	int i = next_fd++;

	fs[i].used = 1;
	fs[i].kind = 1;
	fs[l].pend[fs[l].npend++] = i + 3;
	return i + 3;
}
static void cli_send(int fd, const uint8_t *p, size_t n)
{
	memcpy(F(fd)->tb + F(fd)->tb_len, p, n);
	F(fd)->tb_len += n;
}
static size_t cli_read(int fd, uint8_t *out, size_t max)
{
	struct fsock *s = F(fd);
	size_t k = s->fb_len - s->fb_off;

	k = k < max ? k : max;
	memcpy(out, s->fb + s->fb_off, k);
	s->fb_off += k;
	return k;
}
static int cli_closed(int fd) { return F(fd)->closed; }

/* One pass of net_main's loop, minus net_link_work. */
static void step(int n)
{
	while (n--) {
		fake_now_ms += TICK_MS;
		tick();
		poll_once();
		tx_flush();
	}
}

struct pc {
	int fd;
	struct wl_dir tx, rx;	/* PC -> board, board -> PC */
};

static void hmac_label(const char *label, const uint8_t *a, const uint8_t *b,
		       uint8_t out[32])
{
	uint8_t in[64];
	size_t ll = strlen(label);

	memcpy(in, label, ll);
	memcpy(in + ll, a, 16);
	memcpy(in + ll + 16, b, b == NULL ? 0 : 16);
	wl_hmac_sha256(KEY, 32, in, ll + 16 + (b ? 16 : 0), out);
}

/* The PC end of M1/M2/M3. 0 when the board answered M3 (so keys are set),
 * -1 when it closed or never opened (refused at accept), -2 for a refused
 * M2. */
static int pc_handshake(struct pc *c, int good_m2)
{
	uint8_t m1[WL_M1_LEN], m2[WL_M2_LEN], m3[WL_M3_LEN], salt[32], keys[64];
	uint8_t np[16], tag[32];

	memset(c, 0, sizeof(*c));
	c->fd = cli_connect();
	step(1);
	if (cli_closed(c->fd) || cli_read(c->fd, m1, sizeof(m1)) != sizeof(m1)) {
		return -1;
	}
	memset(np, 0x5a, sizeof(np));
	hmac_label("blink-wifi-1 pc", m1 + 4, np, tag);
	if (!good_m2) {
		tag[0] ^= 1;
	}
	memcpy(m2, np, 16);
	memcpy(m2 + 16, tag, 32);
	cli_send(c->fd, m2, sizeof(m2));
	step(1);
	if (cli_read(c->fd, m3, sizeof(m3)) != sizeof(m3)) {
		return -2;
	}
	memcpy(salt, m1 + 4, 16);
	memcpy(salt + 16, np, 16);
	wl_hkdf_sha256(salt, 32, KEY, 32, (const uint8_t *)"blink-wifi-1 session",
		       20, keys, 64);
	memcpy(c->tx.key, keys, 32);
	memcpy(c->rx.key, keys + 32, 32);
	return 0;
}
static void pc_send(struct pc *c, uint8_t type, const char *line)
{
	uint8_t f[WL_FRAME_MAX];
	int n = wl_frame_seal(&c->tx, type, (const uint8_t *)line,
			      strlen(line), f, sizeof(f));

	cli_send(c->fd, f, (size_t)n);
}
/* The next board line, or NULL. */
static const char *pc_read_line(struct pc *c)
{
	static char line[600];
	uint8_t f[WL_FRAME_MAX], t, b[WL_PT_MAX];
	size_t n = cli_read(c->fd, f, sizeof(f));
	int k;

	if (n == 0) {
		return NULL;
	}
	k = wl_frame_open(&c->rx, f, n, &t, b, sizeof(b));
	if (k < 0 || t != WL_T_LINE) {
		return NULL;
	}
	memcpy(line, b, (size_t)k);
	line[k] = 0;
	return line;
}

static int fails;
static void eq(const char *what, long got, long want)
{
	if (got != want) {
		fails++;
		printf("FAIL: %s (got %ld, want %ld)\n", what, got, want);
	} else {
		printf("PASS: %s\n", what);
	}
}

static void reset(void)
{
	wl_server_close_all();
	memset(fs, 0, sizeof(fs));
	next_fd = 0;
	console[0] = 0;
	fail_stream_socket = fail_dgram_socket = fail_dgram_bind = 0;
	rx_head = rx_tail = tx_head = tx_tail = 0;
	rng_dead = false;
	last_answer = open_after = 0;
	listen_said = disc_said = false;
	paired = true;
	nl_state = NL_UP;
	fake_now_ms += 600000;	/* past any backoff or window of the last case */
	link_auth_ok();
}

static int ok(const char *s)
{
	return wl_verify_body_ok((const uint8_t *)s, strlen(s));
}

static void query(int usock_idx)
{
	struct fsock *s = &fs[usock_idx];
	uint8_t nq[16], tag[32];

	memset(nq, 0x33, sizeof(nq));
	hmac_label("blink-disc-1 q", nq, NULL, tag);
	memcpy(s->q[s->nq].d, "BLQ\x01", 4);
	memcpy(s->q[s->nq].d + 4, nq, 16);
	memcpy(s->q[s->nq].d + 20, tag, 16);
	s->q[s->nq++].n = WL_Q_LEN;
}

int main(void)
{
	struct pc a, b;
	const char *l;
	int u;

	/* --- the helpers of wl_server.h ---------------------------------- */
	eq("the verify line", ok("{\"t\":\"verify\"}"), 1);
	eq("with its newline", ok("{\"t\":\"verify\"}\n"), 1);
	eq("two newlines", ok("{\"t\":\"verify\"}\n\n"), 0);
	eq("a usage line", ok("{\"t\":\"usage\"}"), 0);
	eq("verify with a tail", ok("{\"t\":\"verify\",\"x\":1}"), 0);
	eq("empty", ok(""), 0);
	eq("opener sent whole", wl_opener_sent_whole(20, 20), 1);
	eq("opener short write", wl_opener_sent_whole(7, 20), 0);
	eq("opener EAGAIN", wl_opener_sent_whole(-1, 20), 0);

	/* --- sockets: the failures speak and the discovery socket retries */
	reset();
	fail_stream_socket = 1;
	step(1);
	eq("no listener, no sockets", lsock < 0 && usock < 0, 1);
	eq("a listener that cannot open says so", said("[wifi] listen failed"), 1);
	step(3);
	eq("and says it once, not at each retry", said("[wifi] listen failed"), 1);
	fail_stream_socket = 0;
	step(25);
	eq("the listener comes back on the 1 s schedule", lsock >= 0 && usock >= 0, 1);

	reset();
	fail_dgram_bind = 1;
	step(1);
	eq("the listener is up without discovery", lsock >= 0 && usock < 0, 1);
	eq("a discovery socket that cannot open says so",
	   said("[wifi] discovery socket failed"), 1);
	fail_dgram_bind = 0;
	step(25);
	eq("discovery is retried while the listener is up", usock >= 0, 1);
	eq("without a second listener", next_fd, 3);

	reset();
	paired = false;
	step(2);
	eq("an unpaired board listens on nothing", lsock < 0 && usock < 0, 1);
	paired = true;
	nl_state = NL_OFF;
	step(2);
	eq("and nor does one with no network", lsock < 0 && usock < 0, 1);

	/* --- a session, then D29 ------------------------------------------ */
	reset();
	step(25);
	eq("a good handshake opens a session", pc_handshake(&a, 1) == 0 &&
	   wl_server_session_up(), 1);
	pc_send(&a, WL_T_LINE, "{\"t\":\"ping\"}\n");
	step(3);	/* one recv a pass: the header, then the body */
	{
		uint8_t t, body[600];
		int n = wl_server_rx_frame(&t, body, sizeof(body));

		eq("a sealed line reaches the main thread", n, 13);
	}
	wl_server_tx_line("{\"t\":\"pong\"}", 0);
	step(1);
	l = pc_read_line(&a);
	eq("a queued line leaves sealed", l != NULL && strcmp(l, "{\"t\":\"pong\"}\n") == 0, 1);

	{
		int old = ssock;
		uint32_t gen = wl_server_session_gen();

		eq("a stranger with a wrong proof is refused", pc_handshake(&b, 0), -2);
		eq("the live session is untouched", wl_server_session_up() &&
		   ssock == old && !cli_closed(a.fd), 1);
		eq("and the refusal is said", said("[wifi] handshake refused"), 1);
		eq("and no new generation opened", wl_server_session_gen() == gen, 1);

		/* A stranger that stops after M1 holds the candidate slot, not
		 * the session, and loses it at the 2 s deadline. */
		fake_now_ms += 60000;	/* past the backoff the refusal set */
		b.fd = cli_connect();
		step(1);
		eq("a connection that says nothing leaves the session alone",
		   wl_server_session_up() && ssock == old, 1);
		step(45);
		eq("and is dropped at its deadline", cli_closed(b.fd), 1);

		fake_now_ms += 60000;
		eq("a good proof replaces the session", pc_handshake(&b, 1) == 0 &&
		   wl_server_session_up() && ssock != old && cli_closed(a.fd), 1);
		eq("under a new generation", wl_server_session_gen() != gen, 1);
	}

	/* --- a bad frame ends the session --------------------------------- */
	{
		uint8_t f[WL_FRAME_MAX];
		int n = wl_frame_seal(&b.tx, WL_T_LINE, (const uint8_t *)"{\"t\":\"x\"}\n",
				      10, f, sizeof(f));

		f[n - 1] ^= 1;	/* the tag */
		cli_send(b.fd, f, (size_t)n);
		step(3);
		eq("a frame that fails its tag closes the session",
		   !wl_server_session_up() && cli_closed(b.fd), 1);
		eq("and the close is said", said("[wifi] session closed") >= 2, 1);
	}
	reset();
	step(25);
	pc_handshake(&a, 1);
	{
		uint8_t hdr[2] = { 0xff, 0xff };

		cli_send(a.fd, hdr, 2);
		step(1);
		eq("a header announcing an over-long frame closes it",
		   !wl_server_session_up() && cli_closed(a.fd), 1);
	}

	/* --- the verify-only candidate (D65) ------------------------------ */
	reset();
	step(25);
	link_usb_heard(fake_now_ms);
	link_verify_window(fake_now_ms);
	eq("verify handshake completes", pc_handshake(&a, 1), 0);
	eq("and is not a session", !wl_server_session_up() && !link_wifi_is_up(), 1);
	pc_send(&a, WL_T_LINE, "{\"t\":\"verify\"}\n");
	step(3);
	l = pc_read_line(&a);
	eq("the one verify line is answered", l != NULL && strstr(l, "verified") != NULL, 1);
	eq("then the candidate is closed", cli_closed(a.fd), 1);
	eq("never a session, never routed to proto",
	   !wl_server_session_up() && !link_wifi_is_up() && rx_head == 0, 1);
	eq("verified is said", said("[wifi] verified"), 1);
	link_usb_heard(fake_now_ms);
	eq("the window is shut by it: the next connect is refused",
	   pc_handshake(&b, 1), -1);

	reset();
	step(25);
	link_usb_heard(fake_now_ms);
	link_verify_window(fake_now_ms);
	pc_handshake(&a, 1);
	pc_send(&a, WL_T_LINE, "{\"t\":\"usage\"}\n");
	step(3);
	eq("any other line from a verify-only candidate closes it unanswered",
	   cli_closed(a.fd) && pc_read_line(&a) == NULL && rx_head == 0 &&
	   said("[wifi] verified") == 0, 1);

	reset();
	step(25);
	link_usb_heard(fake_now_ms);	/* USB owns the link, no window */
	eq("with USB active and no window a connection is closed unanswered",
	   pc_handshake(&a, 1), -1);

	/* --- failure backoff ---------------------------------------------- */
	reset();
	step(25);
	pc_handshake(&a, 0);
	eq("after a refusal the next connect is closed at once",
	   pc_handshake(&b, 1), -1);
	fake_now_ms += 1100;
	eq("after the 1 s it is welcome again", pc_handshake(&b, 0), -2);
	fake_now_ms += 1100;
	eq("the second refusal doubles it: 2 s not over", pc_handshake(&a, 1), -1);
	fake_now_ms += 2100;
	eq("and over at 2 s", pc_handshake(&a, 1), 0);

	/* --- discovery ---------------------------------------------------- */
	reset();
	step(25);
	u = usock - 3;
	query(u);
	step(1);
	eq("a query is answered", fs[u].answers, 1);
	query(u);
	step(1);
	eq("a second within a second is not", fs[u].answers, 1);
	fake_now_ms += 1100;
	query(u);
	step(1);
	eq("one after the second is", fs[u].answers, 2);
	link_usb_heard(fake_now_ms);
	fake_now_ms += 1100;
	query(u);
	step(1);
	eq("none while USB owns the link", fs[u].answers, 2);

	printf("%d failure(s)\n", fails);
	return fails ? 1 : 0;
}
