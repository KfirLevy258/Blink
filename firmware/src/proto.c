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
#include "ota.h"
#include "version.h"
#include "cfg_store.h"
#include "usage_view.h"
#include "net_time.h"
#include "version.h"
#include "cfg_store.h"

#define PROTO_VERSION 2
#define PING_INTERVAL_MS 10000
/*
 * The daemon answers every ping with a pong, so silence means it is genuinely
 * gone -- not merely between its 60 s usage polls. Three missed pings (10 s
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
		 "\"board_id\":\"%s\",\"fw\":\"" CLAUGE_FW_VERSION "\","
		 "\"reset\":\"0x%x\"}",
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


/*
 * --- OTA while tethered ---
 *
 * USB-bridge mode has no network of its own: run_usb() never starts
 * net_worker, so ota.c's HTTPS updater is unreachable and the update row did
 * nothing here. The daemon has both an internet connection and the board's
 * USB serial link, so the split is: the board asks and approves, the daemon
 * fetches and writes.
 *
 * The board does NOT receive the image. An earlier revision pushed it down
 * this protocol in base64 chunks and it was hopeless -- 33% encoding overhead
 * on top of a stop-and-wait round trip per chunk, measured at 213 B/s, and
 * then MCUboot still had to swap slot1 into slot0 afterwards (121-357 s by the
 * notes in ui_settings.c). The daemon instead runs esptool against slot0
 * directly, which this project has been doing by hand all along at ~17 KB/s:
 * about 75 s for a 1.3 MB image, and no swap at all because the bytes land
 * where the bootloader already looks.
 *
 * So all that crosses this link is consent. The board sends ota_flash and puts
 * up a "keep it connected" screen; the next thing it experiences is esptool
 * resetting it into the ROM loader.
 *
 * The trade is real and worth stating: writing slot0 in place gives up
 * MCUboot's test-boot and auto-revert, so a bad image needs a reflash rather
 * than recovering by itself. That is acceptable *here specifically* because
 * this path only exists while a machine with esptool is physically cabled to
 * the board -- the recovery is the same cable that caused it.
 */
static struct ota_manifest ota_m;
static bool ota_staged;		/* ota_avail seen: ota_m is valid */

void proto_ota_check(void)
{
	char buf[96];

	ota_staged = false;
	ota_ui_set(OTA_UI_CHECKING, NULL, 0);
	snprintf(buf, sizeof(buf),
		 "{\"t\":\"ota_query\",\"v\":%d,\"cur\":\"%s\"}",
		 PROTO_VERSION, CLAUGE_FW_VERSION);
	emit(buf);
}

bool proto_ota_install(void)
{
	char buf[64];

	if (!ota_staged) {
		return false;
	}
	/*
	 * Say which link this is on BEFORE raising the state: ota_ui_source()
	 * otherwise still reads WIFI (its default) and the progress screen
	 * announces a download that is not happening -- reported 2026-08-21,
	 * where a USB flash was labelled "Over WiFi".
	 */
	ota_ui_set_source(OTA_SRC_USB);

	/*
	 * Record the intent before handing over, so the next boot can report
	 * the outcome. ota_report_outcome() in main.c already does exactly
	 * this for the WiFi path: it compares the stored target against the
	 * running version and shows "Updated to ..." or "Update failed,
	 * previous version restored." Setting it here gets that for free, and
	 * gets the failure case right too -- if esptool dies mid-write the
	 * board comes back on the OLD version and the mismatch says so.
	 */
	cfg_set_ota_state(1, ota_m.version);

	/* Percent stays at 0 throughout: the board cannot see esptool's
	 * progress, and a bar that does not move is worse than no bar. The UI
	 * shows the "keep it connected" wording for this source instead. */
	ota_ui_set(OTA_UI_DOWNLOADING, &ota_m, 0);
	snprintf(buf, sizeof(buf), "{\"t\":\"ota_flash\",\"v\":%d}",
		 PROTO_VERSION);
	emit(buf);
	return true;
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

		/* Flat model key (protocol.py flattens its models list for
		 * us); an absent key leaves the -1 "unknown" default. */
		double mf = -1;

		msg_get_double(json, "fable_pct", &mf);
		usage_view_set_models(mf);
		printk("[usage] session %.0f%% (%ds)  weekly %.0f%% (%ds)\n",
		       sp, (int)ss, wp, (int)ws);
	} else if (strcmp(type, "time") == 0) {
		double epoch = 0, off = 0;

		if (msg_get_double(json, "epoch", &epoch) &&
		    msg_get_double(json, "utc_offset_min", &off) &&
		    epoch > 0 && epoch < 4102444800.0 &&	/* < year 2100 */
		    off >= -840 && off <= 840) {
			net_time_set_manual((int64_t)epoch);
			net_time_set_offset((int32_t)off);
			printk("[proto] host time synced\n");
		}
	} else if (strcmp(type, "pong") == 0) {
		/* Liveness only: last_host_ms was already stamped above. */
	} else if (strcmp(type, "welcome") == 0) {
		printk("[proto] host connected\n");
	} else if (strcmp(type, "ota_avail") == 0) {
		double sz = 0;

		if (msg_get_str(json, "version", ota_m.version,
				sizeof(ota_m.version)) &&
		    msg_get_str(json, "sha256", ota_m.sha256,
				sizeof(ota_m.sha256)) &&
		    msg_get_double(json, "size", &sz) && sz > 0) {
			ota_m.size = (uint32_t)sz;
			ota_staged = true;
			ota_ui_set(OTA_UI_AVAILABLE, &ota_m, 0);
			printk("[proto] daemon offers %s (%u bytes)\n",
			       ota_m.version, ota_m.size);
		} else {
			ota_ui_set(OTA_UI_FAILED, NULL, 0);
		}
	} else if (strcmp(type, "ota_none") == 0) {
		ota_staged = false;
		ota_ui_set(OTA_UI_UP_TO_DATE, NULL, 0);
	} else if (strcmp(type, "ota_error") == 0) {
		ota_staged = false;
		ota_ui_set(OTA_UI_FAILED, NULL, 0);
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
	 * daemon polls every 60 s, so the window must be comfortably longer.
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
