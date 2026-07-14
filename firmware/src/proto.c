#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/ring_buffer.h>
#include <stdio.h>
#include <string.h>

#include "proto.h"
#include "msg_parse.h"
#include "usage_view.h"
#include "net_time.h"

#define PROTO_VERSION 2
#define PING_INTERVAL_MS 10000
/*
 * The daemon answers every ping with a pong, so silence means it is genuinely
 * gone -- not merely between its 300 s usage polls. Three missed pings (10 s
 * apart) is enough to be sure without being twitchy about one dropped line.
 */
#define HOST_TIMEOUT_MS 35000
#define LINE_MAX 512
#define RX_RING_SIZE 1024

static const struct device *const console_dev =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

static char line[LINE_MAX];
static size_t line_len;
static int64_t last_ping_ms;
static int64_t last_host_ms;
static bool host_seen;

/*
 * RX is interrupt-driven, not polled. On a real UART at 115200 baud the 128-byte
 * hardware FIFO holds only ~11 ms of traffic, so any main-loop stall longer than
 * that (an LVGL frame, a TLS handshake) would silently truncate an inbound line.
 * The ISR drains the FIFO into this ring buffer; proto_service() consumes it at
 * whatever pace the main loop happens to run.
 */
RING_BUF_DECLARE(rx_ring, RX_RING_SIZE);
static uint32_t rx_dropped;

static void uart_isr(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	if (!uart_irq_update(dev)) {
		return;
	}

	while (uart_irq_rx_ready(dev)) {
		uint8_t buf[64];
		int n = uart_fifo_read(dev, buf, sizeof(buf));

		if (n <= 0) {
			break;
		}
		if (ring_buf_put(&rx_ring, buf, n) < (uint32_t)n) {
			rx_dropped++;
		}
	}
}

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
		 "{\"t\":\"hello\",\"v\":%d,\"board\":\"cyd\","
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

	last_host_ms = k_uptime_get();
	host_seen = true;
	if (strcmp(type, "usage") == 0) {
		double sp = 0, wp = 0;
		/* Remaining seconds, not the absolute resets_at timestamps: the
		 * board has no wall clock when tethered over USB, so the daemon
		 * does the subtraction. -1 means unknown.
		 */
		double ss = -1, ws = -1;

		msg_get_double(json, "session_pct", &sp);
		msg_get_double(json, "weekly_pct", &wp);
		msg_get_double(json, "session_resets_in_s", &ss);
		msg_get_double(json, "weekly_resets_in_s", &ws);
		usage_view_update(sp, (int32_t)ss, wp, (int32_t)ws);
		printk("[usage] session %.0f%% (%ds)  weekly %.0f%% (%ds)\n",
		       sp, (int)ss, wp, (int)ws);
	} else if (strcmp(type, "time") == 0) {
		double epoch = 0, off = 0;

		if (msg_get_double(json, "epoch", &epoch) &&
		    msg_get_double(json, "utc_offset_min", &off)) {
			net_time_set_manual((int64_t)epoch);
			net_time_set_offset((int32_t)off);
		}
	} else if (strcmp(type, "pong") == 0) {
		/* Liveness only: last_host_ms was already stamped above. */
	} else if (strcmp(type, "welcome") == 0) {
		printk("[proto] host connected\n");
	} else if (strcmp(type, "status") == 0) {
		char st[24] = "";

		msg_get_str(json, "state", st, sizeof(st));
		printk("[proto] host status: %s\n", st);
		usage_view_set_status(strcmp(st, "rate_limited") == 0 ?
				      USAGE_STATUS_STALE : USAGE_STATUS_ERROR);
	}
	/* unknown types ignored */
}

static void drain_rx(void)
{
	uint8_t c;

	while (ring_buf_get(&rx_ring, &c, 1) == 1) {
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

	if (rx_dropped) {
		printk("[proto] rx ring overflow, dropped %u bytes\n", rx_dropped);
		rx_dropped = 0;
	}
}

void proto_init(void)
{
	line_len = 0;

	if (console_dev && device_is_ready(console_dev)) {
		uart_irq_callback_user_data_set(console_dev, uart_isr, NULL);
		uart_irq_rx_enable(console_dev);
	}

	send_hello();
	last_ping_ms = k_uptime_get();
}

bool proto_host_seen(void)
{
	return host_seen;
}

void proto_service(void)
{
	drain_rx();
	int64_t now = k_uptime_get();

	if (now - last_ping_ms >= PING_INTERVAL_MS) {
		send_ping();
		last_ping_ms = now;
	}

	/* If the host goes away, say so. Holding a green dot over numbers that
	 * stopped updating is worse than admitting we are disconnected. The
	 * daemon polls every 300 s, so the window must be comfortably longer.
	 */
	if (host_seen && (now - last_host_ms) > HOST_TIMEOUT_MS) {
		printk("[proto] host went away\n");
		usage_view_set_status(USAGE_STATUS_DISCONNECTED);
		host_seen = false;
	}
}

void proto_resync(void)
{
	send_hello();
}
