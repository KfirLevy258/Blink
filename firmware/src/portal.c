/*
 * Two-stage setup portal: WiFi first, then Claude sign-in.
 *
 * Hand-rolled HTTP/1.1 -- phase 1 on the SoftAP, phase 2 on the home LAN.
 * The whole surface is a few routes and DRAM is the binding constraint (no
 * PSRAM). Each POST blocks until its step finishes and then returns the next
 * page; the only client-side JS is one onsubmit line harvesting the phone's
 * UTC offset (the board cannot fetch a timezone reliably itself).
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

void portal_set_networks(char list[][33], int n)
{
	n_networks = MIN(n, SCAN_MAX);
	for (int i = 0; i < n_networks; i++) {
		strncpy(networks[i], list[i], sizeof(networks[i]) - 1);
		networks[i][sizeof(networks[i]) - 1] = '\0';
	}
}

/*
 * Escape HTML specials. SSIDs are radio input: any neighbor can broadcast
 * "<script>..." as a network name, and it would land verbatim in the
 * phone's browser. Worst case one byte becomes six ("&quot;").
 */
static void html_escape(const char *src, char *dst, size_t dstlen)
{
	size_t o = 0;

	while (src && *src && o + 1 < dstlen) {
		const char *rep = NULL;

		switch (*src) {
		case '<':  rep = "&lt;";   break;
		case '>':  rep = "&gt;";   break;
		case '&':  rep = "&amp;";  break;
		case '"':  rep = "&quot;"; break;
		case '\'': rep = "&#39;";  break;
		}
		if (rep) {
			size_t l = strlen(rep);

			if (o + l + 1 > dstlen) {
				break;
			}
			memcpy(dst + o, rep, l);
			o += l;
		} else {
			dst[o++] = *src;
		}
		src++;
	}
	dst[o] = '\0';
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

/*
 * Read a whole HTTP request -- headers AND body -- into buf.
 *
 * A single recv() is not enough: the AP hands a station one TCP segment at a
 * time, so a form POST arrives as headers first and the "ssid=...&psk=..." body
 * in a later segment. Reading once captured only the headers and left the body
 * empty, which reached the driver as a blank SSID (-EINVAL, mislabelled "radio
 * not ready"). Loop until the headers are in and Content-Length bytes of body
 * have followed, or the peer goes idle (GET requests, which carry no body).
 */
static int recv_request(int sock, char *buf, size_t buflen)
{
	size_t total = 0;
	int hdr_end = -1;
	size_t content_len = 0;
	int64_t deadline = k_uptime_get() + 3000;

	while (total < buflen - 1 && k_uptime_get() < deadline) {
		struct zsock_pollfd p = { .fd = sock, .events = ZSOCK_POLLIN };

		if (zsock_poll(&p, 1, 500) <= 0) {
			/* Idle. If we know no (more) body is owed, we're done;
			 * with Content-Length still unmet, keep waiting -- the
			 * body segment can lag the headers on a busy AP. The
			 * outer deadline still bounds the total wait. */
			if (hdr_end >= 0 &&
			    total >= (size_t)hdr_end + content_len) {
				break;
			}
			continue;
		}

		int n = zsock_recv(sock, buf + total, buflen - 1 - total, 0);

		if (n <= 0) {
			break;
		}
		total += n;
		buf[total] = '\0';

		if (hdr_end < 0) {
			char *e = strstr(buf, "\r\n\r\n");

			if (e) {
				hdr_end = (int)(e - buf) + 4;

				char *cl = strstr(buf, "Content-Length:");

				if (!cl) {
					cl = strstr(buf, "content-length:");
				}
				if (cl) {
					content_len = strtoul(cl + 15, NULL, 10);
				}
			}
		}
		if (hdr_end >= 0 && total >= (size_t)hdr_end + content_len) {
			break;	/* full body received */
		}
	}
	return (int)total;
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
	"color:#08130c;font-size:16px;font-weight:700}.e{color:#e74c3c;font-size:13px;margin-top:8px}" \
	".spin{display:none;position:fixed;inset:0;background:rgba(14,17,22,.94);z-index:9;" \
	"align-items:center;justify-content:center;flex-direction:column}.spin.on{display:flex}" \
	".ring{width:46px;height:46px;border:4px solid #272c34;border-top-color:#2ecc71;" \
	"border-radius:50%;animation:r 1s linear infinite}@keyframes r{to{transform:rotate(360deg)}}" \
	".spin p{margin-top:16px;color:#8a9199;font-size:14px}</style>"

static void send_html(int sock, const char *body_html)
{
	/* Send the headers, then the body, as two writes -- rather than composing
	 * them into one big scratch buffer. On a board with ~3 KB of DRAM to spare,
	 * a 3 KB static compose buffer is memory we don't have; send_all already
	 * paces the body in small chunks regardless. */
	char hdr[128];
	size_t blen = strlen(body_html);
	int n = snprintf(hdr, sizeof(hdr),
		"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
		"Content-Length: %d\r\nConnection: close\r\n\r\n", (int)blen);

	if (n < 0) {
		return;
	}
	n = MIN(n, (int)sizeof(hdr) - 1);

	if (send_all(sock, hdr, n) < 0) {
		return;
	}
	send_all(sock, body_html, blen);
}

/* One shared compose buffer for every page -- the server is single-threaded,
 * so pages are never built concurrently. 3 KB covers the worst case (the
 * sign-in page with a full-length 512-byte authorize URL) and replaces three
 * per-page statics that together cost more DRAM. */
static char page[3072];

static void wifi_page(int sock, const char *err)
{
	/* CSS rides in as a %s argument: its literal '%' (width:100%) inside a
	 * format string is an unknown conversion -- undefined behavior that
	 * happened to render. */
	int n = snprintf(page, sizeof(page),
		"<!doctype html><html><head><meta name=viewport "
		"content='width=device-width,initial-scale=1'><title>Setup</title>%s"
		"</head><body>"
		"<div class=spin id=sp><div class=ring></div><p>Connecting to your network\xE2\x80\xA6</p></div>"
		"<h1>Connect to WiFi</h1>"
		"<p class=s>Step 1 of 2 &middot; choose your network</p>"
		/* The phone is the only party here that knows the local timezone
		 * (the board can't fetch one reliably: hotel-style networks
		 * black-hole plain HTTP for headless clients, seen 2026-07-14).
		 * JS minutes are west-positive; negate to our east-positive. */
		"<form method=POST action=/wifi "
		"onsubmit=\"document.getElementById('sp').className='spin on';"
		"document.getElementById('tzmin').value=-new Date().getTimezoneOffset()\">"
		"<input type=hidden name=tzmin id=tzmin value=''><div class=c>"
		"<label>Network</label><select name=ssid>", CSS);

	if (n_networks == 0) {
		n += snprintf(page + n, sizeof(page) - n, "<option value=''>(none found)</option>");
	}
	char esc[6 * 32 + 1];

	for (int i = 0; i < n_networks && n < (int)sizeof(page) - 300; i++) {
		html_escape(networks[i], esc, sizeof(esc));
		n += snprintf(page + n, sizeof(page) - n, "<option>%s</option>", esc);
	}
	n += snprintf(page + n, sizeof(page) - n,
		"</select>"
		"<label>Password</label><input name=psk type=password placeholder='WiFi password'>"
		"%s%s%s</div><button type=submit>Connect</button></form></body></html>",
		err ? "<div class=e>" : "", err ? err : "", err ? "</div>" : "");

	send_html(sock, page);
}

static void signin_page(int sock, const char *authorize_url, bool err)
{
	/* The poll script is for every OTHER browser showing this page: only
	 * the tab that POSTs the code gets the done page as its response, and
	 * plain HTTP never updates the rest (a phone left on the QR-opened
	 * page while the code was pasted on a computer sat on "Signing in"
	 * forever, user-reported 2026-07-16). */
	snprintf(page, sizeof(page),
		"<!doctype html><html><head><meta name=viewport "
		"content='width=device-width,initial-scale=1'><title>Sign in</title>%s"
		"<script>setInterval(function(){fetch('/status')"
		".then(function(r){return r.text()})"
		".then(function(t){if(t.indexOf('done')==0)location.href='/done'})"
		".catch(function(){})},1000)</script>"
		"</head><body>"
		"<div class=spin id=sp><div class=ring></div><p>Signing in\xE2\x80\xA6</p></div>"
		"<h1>Sign in to Claude</h1>"
		"<p class=s>Step 2 of 2 &middot; WiFi connected \xE2\x9C\x93</p>"
		"<div class=c>"
		"<a class=b href='%s' target=_blank rel=noreferrer>Sign in to Anthropic</a>"
		"<p style='margin:12px 0 0;font-size:15px;color:#e6e8eb'>"
		"Sign in, copy the code Anthropic gives you, and paste it below.</p>"
		"<form method=POST action=/token "
		"onsubmit=\"document.getElementById('sp').className='spin on'\">"
		"<label>Login code</label>"
		"<textarea name=code rows=3 placeholder='paste login code here'></textarea>"
		"%s<button type=submit>Finish setup</button></form></div></body></html>",
		CSS, authorize_url,
		err ? "<div class=e>That code didn't work. Sign in again and retry.</div>" : "");

	send_html(sock, page);
}

static bool resume_flow;

void portal_set_resume(bool token_already_stored)
{
	resume_flow = token_already_stored;
}

/* The phone browser's UTC offset, harvested with the WiFi credentials. */
#define TZMIN_NONE INT32_MIN
static int32_t last_tzmin = TZMIN_NONE;

bool portal_last_tzmin(int32_t *out)
{
	if (last_tzmin == TZMIN_NONE) {
		return false;
	}
	*out = last_tzmin;
	return true;
}

/* Served in reply to POST /wifi, right before the AP is torn down. */
static void ack_page(int sock, const char *ssid)
{
	/* A device that already holds a token never shows the sign-in QR --
	 * promising one here made a healthy re-provision look broken (seen
	 * with a user 2026-07-14). */
	const char *next_step = resume_flow
		? "Watch the device screen. You are already signed in, so the "
		  "usage gauges should appear within a minute.<br><br>"
		: "Watch the device screen. When it shows a new QR code, the device is on "
		  "your WiFi \xE2\x80\x94 scan that code to finish signing in.<br><br>";

	char esc[6 * 32 + 1];

	html_escape(ssid, esc, sizeof(esc));
	snprintf(page, sizeof(page),
		"<!doctype html><html><head><meta name=viewport "
		"content='width=device-width,initial-scale=1'><title>Connecting</title>%s"
		"</head><body><h1>Joining %s\xE2\x80\xA6</h1>"
		"<p class=s>This setup network is shutting down now.</p>"
		"<div class=c><p style='margin:0;font-size:15px'>"
		"%s"
		"If joining fails, reconnect to the setup network to try again."
		"</p></div></body></html>", CSS, esc, next_step);

	send_html(sock, page);
}

/*
 * Served immediately in reply to POST /token, BEFORE the exchange runs. The
 * exchange is 15-25 s of TLS on this CPU with the server blocked; holding
 * the POST open that long is exactly the response that kept dying on real
 * phones (user-reported 2026-07-16). The page then learns the outcome the
 * same way every other open copy does: by polling /status.
 */
static void working_page(int sock)
{
	/* The poll script has to survive the phone missing the "done" window
	 * entirely: browsers suspend timers the moment the screen dims, and
	 * the user is usually watching the DEVICE at this point. If the
	 * board answered us before and now refuses connections, it finished
	 * and rebooted -- say so instead of spinning forever (user-reported
	 * 2026-07-16). */
	snprintf(page, sizeof(page),
		"<!doctype html><html><head><meta name=viewport "
		"content='width=device-width,initial-scale=1'><title>Signing in</title>%s"
		"<script>var ok=0,bad=0;"
		"setInterval(function(){fetch('/status')"
		".then(function(r){return r.text()})"
		".then(function(t){ok++;bad=0;"
		"if(t.indexOf('done')==0)location.href='/done';"
		"else if(t.indexOf('fail')==0)location.href='/'})"
		".catch(function(){bad++;"
		"if(ok>0&&bad>=4){document.body.innerHTML="
		"'<h1>All set \\u2713<\\/h1>"
		"<p class=s>The device finished signing in and restarted. "
		"When its screen shows the gauges, you are done \\u2014 "
		"you can close this page.<\\/p>'}"
		"})},800)</script>"
		"</head><body>"
		"<div class='spin on'><div class=ring></div>"
		"<p>Signing in on the device\xE2\x80\xA6<br>"
		"This page updates by itself.</p></div>"
		"</body></html>", CSS);

	send_html(sock, page);
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
	static const char r[] =
		"HTTP/1.1 302 Found\r\nLocation: /\r\n"
		"Content-Length: 0\r\nConnection: close\r\n\r\n";

	send_all(sock, r, sizeof(r) - 1);
}

static int server_open(void)
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
	    zsock_listen(srv, 4) < 0) {
		zsock_close(srv);
		return -errno;
	}
	zsock_fcntl(srv, F_SETFL, O_NONBLOCK);
	return srv;
}

/* Poll+accept until a client connects or the deadline passes. Pumps the
 * caller's idle hook (LVGL) while waiting. Returns the fd or -ETIMEDOUT. */
static int wait_client(int srv, int64_t deadline)
{
	while (k_uptime_get() < deadline) {
		struct zsock_pollfd pfd = { .fd = srv, .events = ZSOCK_POLLIN };

		if (zsock_poll(&pfd, 1, 100) <= 0) {
			portal_idle_hook();
			continue;
		}

		struct sockaddr_in ca;
		socklen_t clen = sizeof(ca);
		int c = zsock_accept(srv, (struct sockaddr *)&ca, &clen);

		if (c >= 0) {
			return c;
		}
		k_msleep(20);
	}
	return -ETIMEDOUT;
}

int portal_run_wifi(char *ssid, size_t ssid_len, char *psk, size_t psk_len,
		    const char *err_msg, int timeout_s)
{
	int srv = server_open();

	if (srv < 0) {
		return srv;
	}
	printk("[portal] wifi phase on http://%s\n", AP_IP);

	int64_t deadline = k_uptime_get() + (int64_t)timeout_s * 1000;

	for (;;) {
		int c = wait_client(srv, deadline);

		if (c < 0) {
			zsock_close(srv);
			return -ETIMEDOUT;
		}

		int n = recv_request(c, req, sizeof(req));

		if (n <= 0) {
			zsock_close(c);
			continue;
		}

		if (strncmp(req, "POST /wifi", 10) == 0) {
			char *b = strstr(req, "\r\n\r\n");

			ssid[0] = '\0';
			psk[0] = '\0';
			if (b) {
				char tzs[8];

				strncpy(body, b + 4, sizeof(body) - 1);
				body[sizeof(body) - 1] = '\0';
				form_field(body, "ssid", ssid, ssid_len);
				form_field(body, "psk", psk, psk_len);
				if (form_field(body, "tzmin", tzs, sizeof(tzs))) {
					long v = strtol(tzs, NULL, 10);

					/* Real offsets are within +/-14 h;
					 * anything else is a mangled form. */
					if (v >= -840 && v <= 840) {
						last_tzmin = (int32_t)v;
					}
				}
			}
			printk("[portal] wifi ssid=\"%s\" psk=%s tz=%d\n", ssid,
			       psk[0] ? "(set)" : "(empty)",
			       last_tzmin == TZMIN_NONE ? 9999 : (int)last_tzmin);

			if (!ssid[0]) {
				wifi_page(c, "no network selected");
				zsock_close(c);
				continue;
			}

			/* Ack, give the paced send time to reach the phone,
			 * then hand the credentials back -- the caller tears
			 * the AP down and attempts the join. */
			ack_page(c, ssid);
			k_msleep(2000);
			zsock_close(c);
			zsock_close(srv);
			return 0;
		}

		if (strncmp(req, "GET / ", 6) == 0) {
			wifi_page(c, err_msg);
		} else {
			redirect(c);	/* captive-portal probes and the rest */
		}
		zsock_close(c);
	}
}

int portal_run_signin(const char *authorize_url,
		      void (*sign_in_start)(const char *code),
		      int (*sign_in_poll)(void), int timeout_s)
{
	int srv = server_open();

	if (srv < 0) {
		return srv;
	}
	printk("[portal] signin phase up on the LAN\n");

	int64_t deadline = k_uptime_get() + (int64_t)timeout_s * 1000;
	bool err = false;
	bool running = false;			/* exchange on the worker */
	const char *status_now = "wait";	/* what /status answers */

	for (;;) {
		/* Short accept slices, so a finished exchange is noticed
		 * within ~250 ms even with no client knocking. */
		int64_t slice = k_uptime_get() + 250;
		int c = wait_client(srv, MIN(slice, deadline));

		if (running) {
			int rc = sign_in_poll();

			if (rc == 0) {
				printk("[portal] sign-in ok; broadcasting\n");
				running = false;
				status_now = "done";
				if (c >= 0) {
					zsock_close(c);
				}

				/* Grace window: every open copy of the page
				 * polls /status; give them a few rounds to
				 * hear "done" and fetch the done page before
				 * the server goes away. Pages that sleep
				 * through this learn the outcome from the
				 * connection-refused heuristic instead. */
				int64_t grace = k_uptime_get() + 8000;

				for (;;) {
					int c2 = wait_client(srv, grace);

					if (c2 < 0) {
						break;
					}
					if (recv_request(c2, req, sizeof(req)) > 0 &&
					    strncmp(req, "GET /status", 11) == 0) {
						send_html(c2, "done");
					} else {
						done_page(c2);
					}
					zsock_close(c2);
				}
				zsock_close(srv);
				return 0;
			}
			if (rc > 0) {
				/* Only the working page reacts to "fail" (it
				 * bounces back to the form); the form itself
				 * ignores it, so a stale "fail" cannot cause
				 * a redirect loop. */
				running = false;
				err = true;
				status_now = "fail";
			}
		}

		if (c < 0) {
			if (k_uptime_get() >= deadline) {
				zsock_close(srv);
				return -ETIMEDOUT;
			}
			continue;	/* just the end of a slice */
		}

		int n = recv_request(c, req, sizeof(req));

		if (n <= 0) {
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

			/* Answer first, exchange in the background: the
			 * server stays responsive to /status polls the
			 * whole time. */
			working_page(c);
			zsock_close(c);
			if (!running) {
				status_now = "wait";
				sign_in_start(code);
				running = true;
			}
			continue;
		}

		if (strncmp(req, "GET /status", 11) == 0) {
			send_html(c, status_now);
		} else if (strncmp(req, "GET / ", 6) == 0) {
			signin_page(c, authorize_url, err);
		} else {
			redirect(c);
		}
		zsock_close(c);
	}
}
