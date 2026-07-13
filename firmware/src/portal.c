/*
 * Two-stage setup portal: WiFi first, then Claude sign-in.
 *
 * Hand-rolled HTTP/1.1 on the SoftAP -- the whole surface is a few routes and
 * DRAM is the binding constraint (no PSRAM). Each POST blocks until its step
 * finishes and then returns the next page, so the flow needs no client-side JS.
 *
 * Two device quirks are designed around, both learned the hard way:
 *  - Non-blocking accept()+poll, never blocking accept(): the blocking path
 *    faults on this ESP32 build the instant a client connects.
 *  - Responses are sent in small paced chunks: the AP delivers only the first
 *    ~1280-byte TCP segment to a station and then stalls, so a single large
 *    write leaves the phone waiting forever.
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

__weak void portal_idle_hook(void) { }

#define PORT 80
#define REQ_MAX 2048
#define SCAN_MAX 12

static char req[REQ_MAX];
static char body[1024];
static char networks[SCAN_MAX][33];
static int n_networks;
static volatile int conn_count;
static bool wifi_done;

int portal_conn_count(void) { return conn_count; }

void portal_set_networks(char list[][33], int n)
{
	n_networks = MIN(n, SCAN_MAX);
	for (int i = 0; i < n_networks; i++) {
		strncpy(networks[i], list[i], sizeof(networks[i]) - 1);
		networks[i][sizeof(networks[i]) - 1] = '\0';
	}
}

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

/* Paced, chunked send -- see file header. */
static int send_all(int sock, const char *buf, size_t len)
{
	size_t off = 0;
	const size_t CHUNK = 512;

	while (off < len) {
		size_t want = MIN(CHUNK, len - off);
		int n = zsock_send(sock, buf + off, want, 0);

		if (n <= 0) {
			return -1;
		}
		off += n;
		if (off < len) {
			k_msleep(15);
		}
	}
	return (int)off;
}

/* --- shared CSS, small enough to inline on every page --- */
#define CSS \
	"<style>*{box-sizing:border-box}body{margin:0;background:#0e1116;color:#e6e8eb;" \
	"font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;padding:24px 20px}" \
	"h1{font-size:20px;margin:0 0 4px}p.s{color:#8a9199;font-size:13px;margin:0 0 20px}" \
	".c{background:#161a20;border:1px solid #272c34;border-radius:14px;padding:18px;margin-bottom:16px}" \
	"label{display:block;font-size:12px;color:#8a9199;margin:12px 0 5px}" \
	"select,input,textarea{width:100%;padding:12px;border-radius:10px;border:1px solid #303743;" \
	"background:#0e1116;color:#e6e8eb;font-size:16px}" \
	"a.b{display:inline-block;color:#08130c;background:#2ecc71;text-decoration:none;padding:12px 16px;" \
	"border-radius:10px;font-weight:700;font-size:15px}" \
	"button{width:100%;margin-top:20px;padding:15px;border:0;border-radius:12px;background:#2ecc71;" \
	"color:#08130c;font-size:16px;font-weight:700}.e{color:#e74c3c;font-size:13px;margin-top:8px}</style>"

static void send_html(int sock, const char *body_html)
{
	static char page[2560];
	int n = snprintf(page, sizeof(page),
		"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
		"Content-Length: %d\r\nConnection: close\r\n\r\n%s",
		(int)strlen(body_html), body_html);

	send_all(sock, page, n);
}

static void wifi_page(int sock, bool err)
{
	static char b[2048];
	int n = snprintf(b, sizeof(b),
		"<!doctype html><html><head><meta name=viewport "
		"content='width=device-width,initial-scale=1'><title>Setup</title>" CSS
		"</head><body><h1>Connect to WiFi</h1>"
		"<p class=s>Step 1 of 2 &middot; choose your network</p>"
		"<form method=POST action=/wifi><div class=c>"
		"<label>Network</label><select name=ssid>");

	if (n_networks == 0) {
		n += snprintf(b + n, sizeof(b) - n, "<option value=''>(none found)</option>");
	}
	for (int i = 0; i < n_networks && n < (int)sizeof(b) - 300; i++) {
		n += snprintf(b + n, sizeof(b) - n, "<option>%s</option>", networks[i]);
	}
	n += snprintf(b + n, sizeof(b) - n,
		"</select><label>or type a hidden network</label>"
		"<input name=ssid2 autocapitalize=off autocorrect=off placeholder='network name'>"
		"<label>Password</label><input name=psk type=password placeholder='WiFi password'>"
		"%s</div><button type=submit>Connect</button></form></body></html>",
		err ? "<div class=e>Couldn't connect. Check the password and try again.</div>" : "");

	send_html(sock, b);
}

static void signin_page(int sock, const char *authorize_url, bool err)
{
	static char b[1536];

	snprintf(b, sizeof(b),
		"<!doctype html><html><head><meta name=viewport "
		"content='width=device-width,initial-scale=1'><title>Sign in</title>" CSS
		"</head><body><h1>Sign in to Claude</h1>"
		"<p class=s>Step 2 of 2 &middot; WiFi connected \xE2\x9C\x93</p>"
		"<div class=c><a class=b href='%s' target=_blank rel=noreferrer>Sign in to Anthropic</a>"
		"<form method=POST action=/token>"
		"<label>Paste the code you get back</label>"
		"<textarea name=code rows=3 placeholder='paste login code here'></textarea>"
		"%s<button type=submit>Finish setup</button></form></div></body></html>",
		authorize_url,
		err ? "<div class=e>That code didn't work. Try signing in again.</div>" : "");

	send_html(sock, b);
}

static void done_page(int sock)
{
	send_html(sock,
		"<!doctype html><html><head><meta name=viewport "
		"content='width=device-width,initial-scale=1'><title>Done</title>" CSS
		"</head><body><h1>All set \xE2\x9C\x93</h1>"
		"<p class=s>The display is taking over now. You can close this page and "
		"forget the setup network.</p></body></html>");
}

static void redirect(int sock)
{
	char r[128];
	int n = snprintf(r, sizeof(r),
		"HTTP/1.1 302 Found\r\nLocation: http://%s/\r\n"
		"Content-Length: 0\r\nConnection: close\r\n\r\n", AP_IP);

	send_all(sock, r, n);
}

int portal_run(const struct portal_cb *cb, int timeout_s)
{
	int srv = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);

	if (srv < 0) {
		return -errno;
	}

	int on = 1;

	zsock_setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));

	struct sockaddr_in a = {
		.sin_family = AF_INET, .sin_port = htons(PORT), .sin_addr.s_addr = INADDR_ANY,
	};

	if (zsock_bind(srv, (struct sockaddr *)&a, sizeof(a)) < 0 ||
	    zsock_listen(srv, 2) < 0) {
		zsock_close(srv);
		return -errno;
	}
	zsock_fcntl(srv, F_SETFL, O_NONBLOCK);

	wifi_done = false;
	printk("[portal] listening on http://%s\n", AP_IP);

	int64_t deadline = k_uptime_get() + (int64_t)timeout_s * 1000;

	while (k_uptime_get() < deadline) {
		struct zsock_pollfd pfd = { .fd = srv, .events = ZSOCK_POLLIN };

		if (zsock_poll(&pfd, 1, 100) <= 0) {
			portal_idle_hook();
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

		struct zsock_pollfd cp = { .fd = c, .events = ZSOCK_POLLIN };

		zsock_poll(&cp, 1, 2000);
		int n = zsock_recv(c, req, sizeof(req) - 1, 0);

		if (n <= 0) {
			zsock_close(c);
			continue;
		}
		req[n] = '\0';

		if (strncmp(req, "POST /wifi", 10) == 0) {
			char *b = strstr(req, "\r\n\r\n");
			struct portal_result { char ssid[33], psk[65]; } r = {0};
			char typed[33] = "";

			if (b) {
				strncpy(body, b + 4, sizeof(body) - 1);
				body[sizeof(body) - 1] = '\0';
				form_field(body, "ssid", r.ssid, sizeof(r.ssid));
				form_field(body, "psk", r.psk, sizeof(r.psk));
				if (form_field(body, "ssid2", typed, sizeof(typed)) && typed[0]) {
					strncpy(r.ssid, typed, sizeof(r.ssid) - 1);
				}
			}
			printk("[portal] wifi ssid=\"%s\" psk=%s\n", r.ssid,
			       r.psk[0] ? "(set)" : "(empty)");

			int rc = cb->connect_wifi(r.ssid, r.psk);

			if (rc == 0) {
				wifi_done = true;
				signin_page(c, cb->authorize_url, false);
			} else {
				wifi_page(c, true);
			}
			zsock_close(c);
			continue;
		}

		if (strncmp(req, "POST /token", 11) == 0) {
			char *b = strstr(req, "\r\n\r\n");
			static char code[256];

			code[0] = '\0';
			if (b) {
				strncpy(body, b + 4, sizeof(body) - 1);
				body[sizeof(body) - 1] = '\0';
				form_field(body, "code", code, sizeof(code));
			}
			printk("[portal] token code=%s\n", code[0] ? "(set)" : "(empty)");

			int rc = cb->sign_in(code);

			if (rc == 0) {
				done_page(c);
				zsock_close(c);
				zsock_close(srv);
				return 0;
			}
			signin_page(c, cb->authorize_url, true);
			zsock_close(c);
			continue;
		}

		if (strncmp(req, "GET / ", 6) == 0) {
			if (wifi_done) {
				signin_page(c, cb->authorize_url, false);
			} else {
				wifi_page(c, false);
			}
			zsock_close(c);
			continue;
		}

		/* captive-portal probe or anything else -> bounce to / */
		redirect(c);
		zsock_close(c);
	}

	zsock_close(srv);
	return -ETIMEDOUT;
}
