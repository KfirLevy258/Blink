/*
 * The setup portal: a minimal HTTP/1.1 server on the SoftAP.
 *
 * Hand-rolled rather than pulling in a full HTTP server stack. The whole
 * surface is two routes, and DRAM is the binding constraint on this chip --
 * there is not room to spend tens of KB on a general-purpose server.
 *
 * GET  /      -> the setup form (network list, password, OAuth code)
 * POST /save  -> parse it, store it, hand back to the caller
 *
 * Deliberately no captive-portal DNS hijack: the screen shows the IP, which is
 * simpler and has far fewer ways to go wrong across phone OSes.
 */
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>
#include <zephyr/posix/fcntl.h>
#include <zephyr/sys/printk.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "portal.h"
#include "net_wifi.h"

/* Pumped while the portal waits, so the caller can keep LVGL alive and update
 * the setup screen. Weak: overridden in main.c. Default does nothing.
 */
__weak void portal_idle_hook(void) { }

#define PORT 80
#define REQ_MAX 2048
#define SCAN_MAX 12

static char req[REQ_MAX];
static char body[1024];
static char networks[SCAN_MAX][33];
static int n_networks;
static volatile int conn_count;
static volatile int last_rx;
static volatile int last_tx;

int portal_conn_count(void) { return conn_count; }
int portal_last_rx(void) { return last_rx; }
int portal_last_tx(void) { return last_tx; }

void portal_set_networks(char list[][33], int n)
{
	n_networks = MIN(n, SCAN_MAX);
	for (int i = 0; i < n_networks; i++) {
		strncpy(networks[i], list[i], sizeof(networks[i]) - 1);
		networks[i][sizeof(networks[i]) - 1] = '\0';
	}
}

/* Percent-decoding for application/x-www-form-urlencoded. The OAuth code
 * contains '#' and '/', so it always arrives encoded.
 */
static void url_decode(char *dst, size_t dlen, const char *src, size_t slen)
{
	size_t j = 0;

	for (size_t i = 0; i < slen && j + 1 < dlen; i++) {
		if (src[i] == '%' && i + 2 < slen) {
			char hex[3] = { src[i + 1], src[i + 2], 0 };

			dst[j++] = (char)strtol(hex, NULL, 16);
			i += 2;
		} else if (src[i] == '+') {
			dst[j++] = ' ';
		} else {
			dst[j++] = src[i];
		}
	}
	dst[j] = '\0';
}

/* Pull one field out of an urlencoded body. */
static bool form_field(const char *b, const char *key, char *out, size_t olen)
{
	char pat[24];

	snprintf(pat, sizeof(pat), "%s=", key);
	const char *p = strstr(b, pat);

	if (!p) {
		return false;
	}
	p += strlen(pat);
	const char *end = strchr(p, '&');
	size_t len = end ? (size_t)(end - p) : strlen(p);

	url_decode(out, olen, p, len);
	return true;
}

static int send_all(int sock, const char *buf, size_t len)
{
	size_t off = 0;

	while (off < len) {
		int n = zsock_send(sock, buf + off, len - off, 0);

		if (n <= 0) {
			last_tx = off;   /* record how far we got before failing */
			return -1;
		}
		off += n;
	}
	last_tx += off;
	return off;
}

static void send_page(int sock, const char *authorize_url)
{
	ARG_UNUSED(authorize_url);
	last_tx = 0;

	/*
	 * The ENTIRE response (headers + HTML) must fit in ONE TCP segment.
	 * This ESP32 AP delivers the first ~1280-byte segment to a station and
	 * then the send stalls -- a bigger page arrives truncated and the phone
	 * waits forever for the rest. So this is deliberately tiny: no CSS, no
	 * scanned-network dropdown (a typed SSID is more reliable anyway, and the
	 * scan only ever found one or two). Keep it well under 1200 bytes, and
	 * send it in a single write.
	 */
	static char page[1024];
	int blen = snprintf(page, sizeof(page),
		"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
		"Content-Length: %d\r\nConnection: close\r\n\r\n%s", 0, "");
	/* Two-pass so Content-Length is exact: build body first. */
	static char body[768];
	int n = snprintf(body, sizeof(body),
		"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
		"<title>Claude setup</title>"
		"<h2>Claude usage setup</h2>"
		"<form method=POST action=/save>"
		"WiFi name<br><input name=ssid2 style=width:95%% autocapitalize=off autocorrect=off><br><br>"
		"WiFi password<br><input name=psk type=password style=width:95%%><br><br>"
		"Login code<br><textarea name=code rows=3 style=width:95%%></textarea><br><br>"
		"<button type=submit>Save and connect</button></form>");

	blen = snprintf(page, sizeof(page),
		"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
		"Content-Length: %d\r\nConnection: close\r\n\r\n%s", n, body);

	send_all(sock, page, blen);
}

static void send_done(int sock, const char *ssid)
{
	char page[512];
	int n = snprintf(page, sizeof(page),
		"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
		"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
		"<style>body{font-family:-apple-system,system-ui,sans-serif;background:#0e1116;"
		"color:#e6e8eb;padding:24px}</style>"
		"<h2>Saved</h2><p>Connecting to <b>%s</b>. This access point will now shut down "
		"and the display will take over.</p>", ssid);

	send_all(sock, page, n);
}

int portal_run(const char *authorize_url, struct portal_result *out, int timeout_s)
{
	int srv = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);

	if (srv < 0) {
		printk("[portal] socket failed\n");
		return -errno;
	}

	int on = 1;

	zsock_setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));

	struct sockaddr_in a = {
		.sin_family = AF_INET,
		.sin_port = htons(PORT),
		.sin_addr.s_addr = INADDR_ANY,
	};

	if (zsock_bind(srv, (struct sockaddr *)&a, sizeof(a)) < 0 ||
	    zsock_listen(srv, 2) < 0) {
		printk("[portal] bind/listen failed\n");
		zsock_close(srv);
		return -errno;
	}

	/*
	 * Non-blocking + poll, NOT blocking accept(). A blocking accept() goes
	 * through zsock_wait_data() -> k_condvar_wait() on the listening socket,
	 * and on this ESP32 build that path faults (a bad cond.lock pointer) the
	 * instant a client connects -- the board hard-crashed with a load-
	 * prohibited exception every time a phone opened the page. A non-blocking
	 * listener skips that condvar entirely: accept() only ever runs when poll
	 * has already told us a connection is queued.
	 */
	zsock_fcntl(srv, F_SETFL, O_NONBLOCK);

	printk("[portal] listening on http://%s\n", AP_IP);

	int64_t deadline = k_uptime_get() + (int64_t)timeout_s * 1000;

	while (k_uptime_get() < deadline) {
		struct zsock_pollfd pfd = { .fd = srv, .events = ZSOCK_POLLIN };

		if (zsock_poll(&pfd, 1, 100) <= 0) {
			portal_idle_hook();	/* keep LVGL + screen status alive */
			continue;
		}

		struct sockaddr_in ca;
		socklen_t clen = sizeof(ca);
		int c = zsock_accept(srv, (struct sockaddr *)&ca, &clen);

		if (c < 0) {
			k_msleep(20);
			continue;
		}
		conn_count++;
		printk("[portal] conn accepted (%d)\n", conn_count);

		/* Give the request a moment to arrive, then read what is there.
		 * The client socket is left blocking, but recv finds the queued
		 * HTTP request already present, so it never hits the condvar path.
		 */
		struct zsock_pollfd cpfd = { .fd = c, .events = ZSOCK_POLLIN };

		zsock_poll(&cpfd, 1, 2000);

		int n = zsock_recv(c, req, sizeof(req) - 1, 0);

		last_rx = n;
		if (n <= 0) {
			zsock_close(c);
			continue;
		}
		req[n] = '\0';

		/*
		 * Captive-portal detection. The OS fetches a known URL and checks
		 * for an exact response ("Success" on iOS, HTTP 204 on Android).
		 * Anything else means "you are behind a portal" -- so we redirect
		 * everything that is not our own page, and the phone opens the
		 * setup sheet by itself instead of the user typing an IP.
		 */
		if (strncmp(req, "GET / ", 6) != 0 &&
		    strncmp(req, "POST /save", 10) != 0) {
			char r[128];
			int rn = snprintf(r, sizeof(r),
					  "HTTP/1.1 302 Found\r\n"
					  "Location: http://%s/\r\n"
					  "Content-Length: 0\r\n"
					  "Connection: close\r\n\r\n", AP_IP);

			send_all(c, r, rn);
			zsock_close(c);
			continue;
		}

		if (strncmp(req, "POST /save", 10) == 0) {
			const char *b = strstr(req, "\r\n\r\n");

			if (b) {
				strncpy(body, b + 4, sizeof(body) - 1);
				body[sizeof(body) - 1] = '\0';

				char typed[33] = "";

				form_field(body, "ssid", out->ssid, sizeof(out->ssid));
				form_field(body, "psk", out->psk, sizeof(out->psk));
				form_field(body, "code", out->code, sizeof(out->code));

				/* A hand-typed name wins: the scan is unreliable
				 * (hidden SSIDs, weak APs), and the user knows
				 * their own network better than we do.
				 */
				if (form_field(body, "ssid2", typed, sizeof(typed)) &&
				    typed[0]) {
					strncpy(out->ssid, typed, sizeof(out->ssid) - 1);
					out->ssid[sizeof(out->ssid) - 1] = '\0';
				}

				/* Never log psk or code. */
				printk("[portal] got ssid=\"%s\" psk=%s code=%s\n",
				       out->ssid,
				       out->psk[0] ? "(set)" : "(empty)",
				       out->code[0] ? "(set)" : "(empty)");

				send_done(c, out->ssid);
				zsock_close(c);
				zsock_close(srv);
				return 0;
			}
		}

		send_page(c, authorize_url);
		zsock_close(c);
	}

	zsock_close(srv);
	printk("[portal] timed out\n");
	return -ETIMEDOUT;
}
