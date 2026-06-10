#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/sys/printk.h>
#include <stdio.h>
#include <string.h>

#include "proto.h"
#include "msg_parse.h"
#include "usage_view.h"

#define PROTO_VERSION 1
#define PING_INTERVAL_MS 10000
#define LINE_MAX 512

static const struct device *const console_dev =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

static char line[LINE_MAX];
static size_t line_len;
static int64_t last_ping_ms;

static void emit(const char *json)
{
	/* JSON lines go to the same console UART; '\n'-terminated. */
	printk("%s\n", json);
}

static void send_hello(void)
{
	uint8_t id[16];
	char idhex[33] = "unknown";
	ssize_t n = hwinfo_get_device_id(id, sizeof(id));

	if (n > 0) {
		for (ssize_t i = 0; i < n && i < 16; i++) {
			snprintf(idhex + i * 2, 3, "%02x", id[i]);
		}
	}

	uint32_t cause = 0;
	(void)hwinfo_get_reset_cause(&cause);

	char buf[160];
	snprintf(buf, sizeof(buf),
		 "{\"t\":\"hello\",\"v\":%d,\"board\":\"esp32c6\","
		 "\"board_id\":\"%s\",\"fw\":\"0.2.0\",\"reset\":\"0x%x\"}",
		 PROTO_VERSION, idhex, cause);
	emit(buf);
}

static void send_ping(void)
{
	char buf[64];
	snprintf(buf, sizeof(buf), "{\"t\":\"ping\",\"v\":%d,\"up_ms\":%lld}",
		 PROTO_VERSION, (long long)k_uptime_get());
	emit(buf);
}

static void dispatch(const char *json)
{
	char type[16];
	if (!msg_get_str(json, "t", type, sizeof(type))) {
		return; /* not a protocol line */
	}
	if (strcmp(type, "usage") == 0) {
		double sp = 0, wp = 0;
		char sr[40] = "", wr[40] = "";
		msg_get_double(json, "session_pct", &sp);
		msg_get_double(json, "weekly_pct", &wp);
		msg_get_str(json, "session_resets_at", sr, sizeof(sr));
		msg_get_str(json, "weekly_resets_at", wr, sizeof(wr));
		usage_view_update(sp, sr, wp, wr);
	} else if (strcmp(type, "welcome") == 0) {
		printk("[proto] host connected\n");
	} else if (strcmp(type, "status") == 0) {
		char st[24] = "";
		msg_get_str(json, "state", st, sizeof(st));
		printk("[proto] host status: %s\n", st);
	}
	/* unknown types ignored */
}

static void drain_rx(void)
{
	unsigned char c;

	while (console_dev && uart_poll_in(console_dev, &c) == 0) {
		if (c == '\n' || c == '\r') {
			if (line_len > 0) {
				line[line_len] = '\0';
				dispatch(line);
				line_len = 0;
			}
		} else if (line_len < LINE_MAX - 1) {
			line[line_len++] = (char)c;
		} else {
			line_len = 0; /* overflow: drop the line */
		}
	}
}

void proto_init(void)
{
	line_len = 0;
	send_hello();
	last_ping_ms = k_uptime_get();
}

void proto_service(void)
{
	drain_rx();
	int64_t now = k_uptime_get();

	if (now - last_ping_ms >= PING_INTERVAL_MS) {
		send_ping();
		last_ping_ms = now;
	}
}
