/*
 * Timezone lookup: one small HTTP GET to ip-api.com.
 *
 * Plain HTTP on purpose -- an extra TLS context here would cost tens of KB of
 * RAM for a value that is not secret and is cross-checked against nothing.
 * Worst case a tampered reply shows a wrong wall clock; usage data and OAuth
 * never touch this path.
 */
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>
#include <zephyr/sys/printk.h>
#include <string.h>

#include "tz_fetch.h"
#include "msg_parse.h"

#define HOST "ip-api.com"

int tz_fetch_offset(int32_t *offset_min)
{
	struct zsock_addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM };
	struct zsock_addrinfo *res = NULL;

	if (zsock_getaddrinfo(HOST, "80", &hints, &res) != 0 || !res) {
		printk("[tz] dns failed\n");
		return -EIO;
	}

	int sock = zsock_socket(res->ai_family, res->ai_socktype, IPPROTO_TCP);

	if (sock < 0) {
		zsock_freeaddrinfo(res);
		return -EIO;
	}

	/* Which address DNS handed us: a wrong or poisoned A record and a
	 * filtered port 80 both present as "0 bytes back", and only this line
	 * tells them apart (chasing exactly that on 2026-07-14). */
	char ipstr[INET_ADDRSTRLEN] = "?";

	zsock_inet_ntop(AF_INET,
			&((struct sockaddr_in *)res->ai_addr)->sin_addr,
			ipstr, sizeof(ipstr));
	printk("[tz] resolved %s -> %s\n", HOST, ipstr);

	struct timeval tv = { .tv_sec = 8 };

	zsock_setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
	zsock_setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

	int rc = zsock_connect(sock, res->ai_addr, res->ai_addrlen);

	zsock_freeaddrinfo(res);
	if (rc != 0) {
		zsock_close(sock);
		printk("[tz] connect failed\n");
		return -EIO;
	}

	static const char req[] =
		"GET /json/?fields=offset HTTP/1.1\r\n"
		"Host: " HOST "\r\n"
		"Connection: close\r\n\r\n";

	if (zsock_send(sock, req, sizeof(req) - 1, 0) < 0) {
		zsock_close(sock);
		return -EIO;
	}

	/* Whole response (headers + ~20-byte body) fits comfortably. */
	char buf[512];
	int len = 0, n;

	while (len < (int)sizeof(buf) - 1 &&
	       (n = zsock_recv(sock, buf + len, sizeof(buf) - 1 - len, 0)) > 0) {
		len += n;
	}
	if (n < 0 && len == 0) {
		/* errno tells timeout (EAGAIN: nothing ever came, port
		 * filtered?) from reset (ECONNRESET: something answered
		 * with a slammed door). */
		printk("[tz] recv failed: errno %d\n", errno);
	}
	zsock_close(sock);
	buf[len] = '\0';

	/* Parse only the body: headers are not ours to grep for JSON keys. */
	const char *body = strstr(buf, "\r\n\r\n");
	double off_s;

	if (body == NULL || !msg_get_double(body, "offset", &off_s)) {
		/* Public data, so the reply's start is safe to log -- and it is
		 * the only way to tell "empty read" from "unexpected shape"
		 * (bitten on hardware 2026-07-14: the bare message could not
		 * distinguish the two). */
		printk("[tz] no offset in reply (%d bytes: \"%.32s\")\n", len, buf);
		return -EBADMSG;
	}
	/* Real offsets are within +/-14 h. The transport is plain HTTP, so a
	 * tampered or garbage value (1e300, nan) must not reach the int cast
	 * or NVS. */
	if (!(off_s >= -840.0 * 60 && off_s <= 840.0 * 60)) {
		printk("[tz] offset out of range\n");
		return -EBADMSG;
	}
	*offset_min = (int32_t)(off_s / 60.0);
	printk("[tz] utc offset %d min\n", (int)*offset_min);
	return 0;
}
