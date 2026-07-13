/*
 * Captive-portal DNS: answer every A query with our own address.
 *
 * This is what makes the setup page open by itself. On joining a network, phones
 * probe a known URL (captive.apple.com/hotspot-detect.html on iOS,
 * /generate_204 on Android) to decide whether the network has real internet.
 * Hijacking DNS points that probe at us; portal.c then answers it with something
 * other than the expected "success", and the OS concludes it is behind a captive
 * portal and pops the page up on its own.
 *
 * Without the DNS hijack the probe never resolves, the OS reports "no internet",
 * and the user has to type an IP address by hand.
 */
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>
#include <zephyr/sys/printk.h>
#include <string.h>

#include "dns_hijack.h"
#include "net_wifi.h"

#define DNS_PORT 53
#define BUF_MAX 320
#define STACK_SIZE 2048

static K_THREAD_STACK_DEFINE(dns_stack, STACK_SIZE);
static struct k_thread dns_thread;
static volatile bool running;

static void dns_loop(void *a, void *b, void *c)
{
	ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);

	static uint8_t buf[BUF_MAX];
	int s = zsock_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);

	if (s < 0) {
		printk("[dns] socket failed\n");
		return;
	}

	struct sockaddr_in me = {
		.sin_family = AF_INET,
		.sin_port = htons(DNS_PORT),
		.sin_addr.s_addr = INADDR_ANY,
	};

	if (zsock_bind(s, (struct sockaddr *)&me, sizeof(me)) < 0) {
		printk("[dns] bind failed\n");
		zsock_close(s);
		return;
	}

	struct in_addr self;

	net_addr_pton(AF_INET, AP_IP, &self);

	struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };

	zsock_setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

	printk("[dns] hijacking all lookups -> %s\n", AP_IP);

	while (running) {
		struct sockaddr_in from;
		socklen_t flen = sizeof(from);
		int n = zsock_recvfrom(s, buf, sizeof(buf), 0,
				       (struct sockaddr *)&from, &flen);

		/* 12-byte header + at least a 1-byte name + qtype/qclass. */
		if (n < 17 || n > (int)sizeof(buf) - 16) {
			continue;
		}

		/* Only answer standard queries with exactly one question. */
		uint16_t qdcount = (buf[4] << 8) | buf[5];

		if ((buf[2] & 0x80) || qdcount != 1) {
			continue;
		}

		/* Walk the QNAME labels to find where the question ends. */
		int p = 12;

		while (p < n && buf[p] != 0) {
			p += buf[p] + 1;
		}
		p += 1 + 4;	/* root label + QTYPE + QCLASS */
		if (p > n) {
			continue;
		}

		/* Turn the query into an answer, in place. */
		buf[2] = 0x84;	/* QR=1, AA=1 */
		buf[3] = 0x00;
		buf[6] = 0x00;	/* ANCOUNT = 1 */
		buf[7] = 0x01;
		buf[8] = buf[9] = buf[10] = buf[11] = 0;

		uint8_t *ans = &buf[p];

		*ans++ = 0xC0;	/* name: pointer back to the question at offset 12 */
		*ans++ = 0x0C;
		*ans++ = 0x00; *ans++ = 0x01;	/* TYPE  A   */
		*ans++ = 0x00; *ans++ = 0x01;	/* CLASS IN  */
		*ans++ = 0x00; *ans++ = 0x00;	/* TTL 30s: short, so the phone */
		*ans++ = 0x00; *ans++ = 0x1E;	/* re-resolves once we are gone */
		*ans++ = 0x00; *ans++ = 0x04;	/* RDLENGTH = 4 */
		memcpy(ans, &self.s_addr, 4);
		ans += 4;

		zsock_sendto(s, buf, ans - buf, 0, (struct sockaddr *)&from, flen);
	}

	zsock_close(s);
}

void dns_hijack_start(void)
{
	if (running) {
		return;
	}
	running = true;
	k_thread_create(&dns_thread, dns_stack, STACK_SIZE, dns_loop,
			NULL, NULL, NULL, K_PRIO_PREEMPT(7), 0, K_NO_WAIT);
	k_thread_name_set(&dns_thread, "dns_hijack");
}

void dns_hijack_stop(void)
{
	running = false;
}
