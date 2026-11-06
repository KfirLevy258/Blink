#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/ring_buffer.h>
#include <stdio.h>
#include <string.h>

#include "proto.h"
#include "link.h"
#include "net_link.h"
#include "wifi_msg.h"
#include <mbedtls/sha256.h>
#include "msg_parse.h"
#include "ota.h"
#include "version.h"
#include "cfg_store.h"
#include "whatsnew.h"
#include "usage_view.h"
#include "usage_state.h"
#include "usage_freshness.h"
#include "svc_state.h"
#include "net_time.h"
#include "version.h"
#include "cfg_store.h"
#include "theme.h"
#include "ui_theme.h"
#include "ui_settings.h"
#include "touch_inject.h"
#if defined(CONFIG_BLINK_WIFI_LINK)
#include <errno.h>
#include "wl_frame.h"
#include "wl_server.h"
#include "ota_net.h"
#include <zephyr/sys/reboot.h>
#include "ui_boot.h"
#endif

#define PROTO_VERSION BLINK_PROTO_VERSION
#define PING_INTERVAL_MS 10000
/*
 * The daemon answers every ping with a pong, so silence means it is genuinely
 * gone -- not merely between its 60 s usage polls. Three missed pings (10 s
 * apart) is enough to be sure without being twitchy about one dropped line.
 */
/* Three missed pongs (the board pings every 10 s). Past this a host that
 * has not said goodbye is taken to be asleep -- see proto_host_lost().
 * One definition, in link.h: link arbitration uses the same number. */
#define HOST_TIMEOUT_MS LINK_HOST_TIMEOUT_MS
#define LINE_MAX 512
/*
 * Sized to the largest thing the daemon sends without waiting for an answer,
 * not to a line. That is its reply to a hello: welcome, a waiting ota_error,
 * time, svc for both pages, usage, session and an update offer, back to back.
 * tests/pc/test_hello_burst.py builds it through the real Bridge at its
 * widest -- the usage line counted at its 512-byte cap -- and measures 1266
 * bytes. The daemon review's figure was about 1095. Either is over 1024.
 *
 * Whether the old ring overflowed is a race and not a certainty: at 115200
 * baud a byte lands every 87 us, so 1024 bytes is 89 ms of line, and the
 * ring only fills if the main loop stops draining for that long while the
 * burst is in flight -- and the burst is timed to find the one moment it
 * plausibly does. The second boot hello goes out from main.c just before
 * run_usb(); the reply's usage line makes the next lv_timer_handler() paint
 * the first full gauge frame, and the rest of the burst arrives behind it.
 * Nobody has timed that frame on a board. Whatever arrives while the ring is
 * full the ISR drops, which at the tail of this burst is the update offer the
 * Settings row is waiting on -- and if its newline goes too, the next line
 * is glued onto what is left of it and lost as well.
 *
 * Draining harder was the alternative, and it has no single place to go: the
 * stalls are frame renders inside LVGL, a TLS handshake, the boot clip. A
 * ring that holds the whole burst makes the length of any stall irrelevant to
 * it, up to the daemon's next fast poll two seconds later: the burst is
 * finite, and nothing but a pong follows it. 2048 holds the measured worst
 * case with 782 bytes over, and costs exactly the 1 KB it adds: the ring
 * links into dram1_0_seg, which went from 54532 to 55556 of 98304 bytes
 * between the develop build and this one (2026-10-27), 42 KB still free.
 */
#define RX_RING_SIZE 2048

static const struct device *const console_dev =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

static char line[LINE_MAX];
static size_t line_len;

/*
 * A number off the wire, or the default it arrived with, and never a value
 * outside the range the field can mean.
 *
 * strtod happily returns nan, inf and 1e300, and every one of those was cast
 * straight to an int -- undefined behaviour in C, and on this soft-float
 * target a saturated or arbitrary integer on the panel. The `time` handler
 * has always bounded its two fields; the usage fields did not. Anything that
 * is not a real number in range leaves the default in place, which every
 * field here defines as "unknown" or "none".
 */
#define SECS_MAX 2147483647.0
/*
 * Percentages can legitimately exceed 100: extra usage carries a weekly window
 * past its limit. The ceiling used to be 100, and num() does not clamp -- it
 * refuses -- so 102 left the caller's `double wp = 0` untouched and the panel
 * drew an empty ring at the moment the user went over. Accept the real number
 * and let usage_view saturate the ring.
 */
#define PCT_MAX 1000.0

static void num(const char *json, const char *key, double *out,
		double lo, double hi)
{
	double v;

	if (!msg_get_double(json, key, &v)) {
		return;
	}
	if (v != v || v < lo || v > hi) { /* nan, or out of range */
		return;
	}
	*out = v;
}
static int64_t last_ping_ms;
static int64_t last_host_ms;
static bool host_seen;
static bool host_bye;		/* the app said it is going, on purpose */
static bool host_lost;		/* silence past the timeout, without a bye */
static char host_ver[16];	/* the daemon's release version, from welcome */
static int host_proto;		/* ...and the protocol it speaks */
/* The last welcome listed "theme" in its caps: the daemon can open the theme
 * builder, so Settings may offer "Edit on computer". */
static bool host_can_theme;

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
	/* Protocol lines only; link_emit picks USB or WiFi (spec 5.3). */
	link_emit(json);
}

/*
 * What this board can show, on every hello and every pref (theme contract,
 * Addendum 3). The daemon sends svc lines only to a board that lists "svc",
 * and theme lines -- and an ok:1 to theme_edit -- only to one that lists
 * "theme". It used to decide by version, fw >= 1.4.0, while this firmware
 * was still numbered 1.3.4: a board built from it was sent neither, and a
 * release bumped to anything short of 1.4.0 would have shipped both features
 * dead (final review, 2026-10-27). A build says what it contains; a number
 * only implies it.
 *
 * On pref as well as hello because hello goes out at boot and nowhere else,
 * and pref is this board's answer to a welcome: a daemon that restarts
 * under a running board hears no hello, only the pref. Older daemons read
 * neither field. Append-only, like THEME_IDS.
 */
/* edit_done: this firmware handles theme_edit done:1 (theme contract
 * Addendum 6). A cap of its own because the 1.4.0 builds before the handler
 * already said "theme", and the daemon must not send them the line. */
#define BOARD_CAPS "\"caps\":[\"svc\",\"theme\",\"edit_done\",\"wifi\"]"

/*
 * The board's id, as hello and pref both say it: hwinfo's device id in hex,
 * 12 digits on this ESP32 (its 6-byte MAC), 32 at most, "unknown" if the
 * driver has none. Kept once read, since it cannot change while the board
 * runs -- but only a real id is kept. "unknown" is asked again next time,
 * so a read that failed once does not name the board "unknown" until the
 * next reboot. The ESP32's eFuse MAC read cannot fail that way; another
 * board's driver might.
 */
static const char *board_id_hex(void)
{
	static char idhex[33];

	if (idhex[0] == '\0') {
		uint8_t id[16];
		ssize_t n = hwinfo_get_device_id(id, sizeof(id));

		if (n <= 0) {
			return "unknown";
		}
		for (ssize_t i = 0; i < n && i < 16; i++) {
			snprintf(idhex + i * 2, 3, "%02x", id[i]);
		}
	}
	return idhex;
}

void proto_send_pref(void)
{
	/*
	 * The board's id rides on every pref, not only on hello (theme
	 * contract, Addendum 5). hello goes out at boot and nowhere else, so a
	 * daemon that restarts under a running board heard only this pref and
	 * had nothing but board.json's memory to say which board it was --
	 * and a remembered id must never decide whose theme wins.
	 *
	 * 119 bytes at its widest ("claude", a 32-digit id and the three
	 * caps; 99 with this board's 12 digits), measured by
	 * tests/pc/test_board_caps.py, which fails if it outgrows this. It was
	 * 96 B for the 61 B line before the id, and 107 before edit_done.
	 * With the NUL that leaves 8 B of the 128. A cap costs its name's
	 * length plus 3 B (the comma and two quotes), so a name of five
	 * characters or fewer still fits and a longer one needs this buffer
	 * made bigger first. Both stay far under the daemon's 512.
	 */
	char buf[128];

	snprintf(buf, sizeof(buf),
		 "{\"t\":\"pref\",\"v\":%d,\"provider\":\"%s\","
		 "\"board_id\":\"%s\"," BOARD_CAPS "}",
		 PROTO_VERSION,
		 cfg_get_main_src() == CFG_MAIN_SRC_CODEX ? "codex" : "claude",
		 board_id_hex());
	emit(buf);
}

static void send_hello(void)
{
	const char *idhex = board_id_hex();

	uint32_t cause = 0;
	(void)hwinfo_get_reset_cause(&cause);

	/* 148 bytes at its widest (a 32-digit id, reset 0xffffffff, fw
	 * "1.4.0", the three caps), measured by tests/pc/test_board_caps.py,
	 * which fails if it outgrows this; a truncated hello is not JSON. With
	 * the NUL, 11 B of the 160 are spare. A cap costs its name's length
	 * plus 3 B and a two-digit minor in fw 1 B, and whatever is added has
	 * to sum to 11 or less. */
	char buf[160];
	snprintf(buf, sizeof(buf),
		 "{\"t\":\"hello\",\"v\":%d,\"board\":\"cyd\","
		 "\"board_id\":\"%s\",\"fw\":\"" BLINK_FW_VERSION "\","
		 "\"reset\":\"0x%x\"," BOARD_CAPS "}",
		 PROTO_VERSION, idhex, cause);
	emit(buf);
	/* Straight after hello: a daemon that starts later, or restarts, has
	 * no other way to learn a preference the user set while it was gone. */
	proto_send_pref();
	proto_send_theme_now("boot");
}

/*
 * Which theme this board has stored. The daemon compares it with the theme
 * the user saved on the computer (~/.blink/theme.json) and sends that one if
 * they differ, and the builder opens on it. A preview is deliberately not
 * reported: it is the builder's own state, and reporting it would make the
 * daemon "restore" an unsaved preview as if it were the saved theme.
 *
 * `why` (contract Addendum 2): "boot" answering a hello or welcome, "user"
 * when the owner chose on the board, "saved" after storing the PC's save.
 * The daemon adopts the board's theme ONLY on "user". Without the reason, a
 * board answering hello with its stored theme looked exactly like an owner
 * choosing it, and the daemon overwrote the theme saved on the computer.
 *
 * A custom theme adds "h", which says WHICH custom theme -- see
 * theme_custom_hash().
 */
void proto_send_theme_now(const char *why)
{
	char buf[80];
	int id = ui_theme_stored_id();
	struct theme_custom t;

	if (id == THEME_CUSTOM && ui_theme_custom_stored(&t)) {
		char h[9];

		theme_custom_hash(&t, h);
		snprintf(buf, sizeof(buf),
			 "{\"t\":\"theme_now\",\"v\":%d,\"id\":%d,"
			 "\"why\":\"%s\",\"h\":\"%s\"}",
			 PROTO_VERSION, id, why, h);
	} else {
		snprintf(buf, sizeof(buf),
			 "{\"t\":\"theme_now\",\"v\":%d,\"id\":%d,"
			 "\"why\":\"%s\"}",
			 PROTO_VERSION, id, why);
	}
	emit(buf);
}

void proto_send_theme_edit(void)
{
	char buf[40];

	snprintf(buf, sizeof(buf), "{\"t\":\"theme_edit\",\"v\":%d}",
		 PROTO_VERSION);
	emit(buf);
}

bool proto_host_can_theme(void)
{
	return host_can_theme;
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
 * The board has no network of its own (the standalone WiFi mode, which
 * did, was removed on 2026-10-27), so ota.c's HTTPS updater is
 * unreachable and the update row used to do nothing here. The daemon has
 * both an internet connection and the board's USB serial link, so the split
 * is: the board asks and approves, the daemon fetches and writes.
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
/* The daemon version this release also carries, when it is newer than the one
 * on the cable. Empty otherwise. The confirmation screen says so, because one
 * tap is about to install both halves. */
static char ota_app[16];

const char *proto_ota_app_version(void)
{
	return ota_app;
}

bool proto_ota_staged(void)
{
	return ota_staged;
}

void proto_ota_check(void)
{
	char buf[96];

	ota_staged = false;
	ota_app[0] = '\0';
	ota_ui_set(OTA_UI_CHECKING, NULL, 0);
	snprintf(buf, sizeof(buf),
		 "{\"t\":\"ota_query\",\"v\":%d,\"cur\":\"%s\"}",
		 PROTO_VERSION, BLINK_FW_VERSION);
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
	ota_ui_set_source(link_active(k_uptime_get()) == LINK_WIFI ?
			  OTA_SRC_WIFI : OTA_SRC_USB);

	/*
	 * The breadcrumb is NOT written here, at consent. It is written when
	 * the daemon says the write is starting (ota_begin below).
	 *
	 * It used to be written here, and that is wrong for a pair update: the
	 * daemon replaces itself first, and the new process opening the serial
	 * port resets this board. It would boot, find a breadcrumb naming a
	 * version it is not running, and announce "Update failed, previous
	 * version restored." before the firmware install had begun -- then
	 * spend the breadcrumb doing it, so the real success went unreported.
	 */

	/* Percent stays at 0 throughout: the board cannot see esptool's
	 * progress, and a bar that does not move is worse than no bar. The UI
	 * shows the "keep it connected" wording for this source instead. */
	ota_ui_set(OTA_UI_DOWNLOADING, &ota_m, 0);
	snprintf(buf, sizeof(buf), "{\"t\":\"ota_flash\",\"v\":%d}",
		 PROTO_VERSION);
	emit(buf);
	return true;
}

#if defined(CONFIG_BLINK_WIFI_LINK)
/*
 * --- OTA over the WiFi session (spec S9) ---
 *
 * The board writes its own slot 1 here. Everything below runs on the main
 * thread: the network thread only moves the O frames into the ring, and
 * drain_wifi() hands them to ota_o_sink() in order behind the ota_begin that
 * precedes them (D34), so a chunk can never be handled before its begin nor
 * ota_end overtake the last chunk. Nothing here logs a byte or an offset.
 */
static uint32_t ota_gen;	/* wl_server_session_gen() the stream began under */
static uint32_t ota_prev;	/* bytes accepted before the current chunk */
static uint8_t ota_pct = 255;

static void ota_ack(uint32_t off, const char *err)
{
	char buf[96];

	if (err) {
		snprintf(buf, sizeof(buf),
			 "{\"t\":\"ota_ack\",\"v\":%d,\"off\":%u,\"err\":\"%s\"}",
			 PROTO_VERSION, (unsigned)off, err);
	} else {
		snprintf(buf, sizeof(buf),
			 "{\"t\":\"ota_ack\",\"v\":%d,\"off\":%u}",
			 PROTO_VERSION, (unsigned)off);
	}
	emit(buf);
}

/* The panel's own sentence says the current version keeps running (the
 * DOWNLOADING -> FAILED notice in ui_settings.c); this is the reason under it. */
static void ota_wifi_failed(const char *why)
{
	ota_ui_set(OTA_UI_FAILED, NULL, 0);
	ota_ui_set_error(why);
}

static void ota_wifi_begin(const char *json)
{
	char v[16];
	struct ota_ui snap;

	ota_ui_get(&snap);
	/* Only a stream the owner consented to, for the version on offer. Size
	 * and sha256 are the ota_avail manifest's, never the daemon's here. */
	if (!ota_staged || snap.st != OTA_UI_DOWNLOADING ||
	    ota_ui_source() != OTA_SRC_WIFI ||
	    !msg_get_str(json, "version", v, sizeof(v)) ||
	    strcmp(v, ota_m.version) != 0) {
		ota_ack(0, "refused");
		return;
	}
	int rc = ota_net_begin(&ota_m);

	if (rc != 0) {
		ota_ack(0, rc == -EBUSY ? "unconfirmed" : "refused");
		return;
	}
	ota_gen = wl_server_session_gen();
	ota_prev = 0;
	ota_pct = 255;
	printk("[proto] ota: writing %s\n", v);
	ota_ack(0, NULL);
}

/* Anything that ends a stream the board did not end itself. */
static void ota_wifi_gone(void)
{
	ota_net_abort();
	ota_wifi_failed("connection lost");
}

/* O frame: offset[4, big-endian] || image bytes. With no stream open (one
 * just aborted, its window still in flight) it is dropped: the daemon was
 * already told with an err ack, or its session is gone. */
static void ota_o_sink(const uint8_t *body, size_t n)
{
	if (!ota_net_active()) {
		return;
	}
	if (wl_server_session_gen() != ota_gen) {
		ota_wifi_gone();
		return;
	}
	if (n < 5) {
		ota_wifi_failed("bad frame");
		ota_net_abort();
		ota_ack(0, "failed");
		return;
	}

	uint32_t off = (uint32_t)body[0] << 24 | (uint32_t)body[1] << 16 |
		       (uint32_t)body[2] << 8 | body[3];
	int got = ota_net_chunk(off, body + 4, n - 4);

	if (got < 0) {
		/* ota_net_chunk has already dropped the stream. */
		ota_wifi_failed("image refused");
		ota_ack(0, "failed");
		return;
	}
	if (ota_ui_source() == OTA_SRC_WIFI && ota_m.size != 0) {
		uint8_t pct = (uint8_t)((uint64_t)got * 100 / ota_m.size);

		if (pct != ota_pct) {
			ota_pct = pct;
			ota_ui_set(OTA_UI_DOWNLOADING, NULL, pct);
		}
	}
	if (ota_net_should_ack(ota_prev, (uint32_t)got, ota_m.size)) {
		ota_ack((uint32_t)got, NULL);
	}
	ota_prev = (uint32_t)got;
}

static void ota_wifi_end(void)
{
	if (!ota_net_active() || wl_server_session_gen() != ota_gen) {
		ota_net_abort();
		ota_wifi_failed("connection lost");
		return;
	}
	if (ota_net_end(&ota_m) != OTA_OK) {
		ota_wifi_failed("image check failed");
		ota_ack(0, "failed");
		return;
	}

	char trail[CFG_OTA_VER_MAX];

	/* Here, not at ota_begin: a stream that is cut leaves no breadcrumb to
	 * report a revert that never happened. */
	whatsnew_trail(BLINK_FW_VERSION, ota_m.version, trail, sizeof(trail));
	cfg_set_ota_state(1, trail);
	ota_ui_set(OTA_UI_REBOOTING, &ota_m, 100);
	k_sleep(K_SECONDS(1));	/* let the panel paint it */
	ui_boot_mark_intentional_reboot();
	sys_reboot(SYS_REBOOT_COLD);
}

/* Once per proto_service(), before the frames are drained, so a replaced
 * session's stream dies before any frame of the new one is read. */
static void ota_wifi_service(void)
{
	bool was = ota_net_active();

	ota_net_service(link_wifi_must_close(k_uptime_get()),
			wl_server_session_up(),
			wl_server_session_gen() != ota_gen);
	if (was && !ota_net_active()) {
		ota_wifi_failed("connection lost");
	}
}
#endif /* CONFIG_BLINK_WIFI_LINK */

/* ---- WiFi pairing over the cable (spec S5.3, S6.1) ---------------------- */

/* Where a protocol line came from. wifi_scan/wifi_set/wifi_forget act only
 * from the cable: pairing is a physical act, and a stranger on the network
 * must not be able to repoint the board (S5.3). */
enum proto_src { PROTO_SRC_USB, PROTO_SRC_WIFI };

#define WIFI_PORT 49731			/* D18 */
#define WIFI_JOIN_DEADLINE_MS 60000
#define WIFI_REQ_DEADLINE_MS 150000	/* queued behind a 30 s join, with room */
#define WIFI_WHY_BAD_INPUT "Invalid WiFi settings"
#define WIFI_WHY_SAVE "Could not save the pairing"

/* The candidate of a wifi_set in flight. Persisted only when the join comes
 * up (D21), so a wrong password leaves the old pairing exactly as it was. */
static struct {
	bool active;
	uint32_t ticket;	/* what net_link_request_join returned */
	bool started;
	int64_t req_deadline;	/* the request never started (queued, refused) */
	int64_t deadline;	/* once started: scan 10 s + join 30 s, with room */
	uint8_t ssid[32];
	size_t ssid_len;
	char psk[CFG_PSK_MAX];
	uint8_t key[CFG_PAIR_KEY_LEN];
} cand;

/* True once net_link is running OUR ticket. Before that its state and
 * result belong to another join (the boot join, a previous wifi_set), and
 * after it to a later one; neither may be credited to the candidate (D21). */
static bool cand_started(void)
{
	if (cand.active && !cand.started && cand.ticket != 0 &&
	    net_link_run_ticket() == cand.ticket) {
		cand.started = true;
		cand.deadline = k_uptime_get() + WIFI_JOIN_DEADLINE_MS;
	}
	return cand.active && cand.started;
}

static int scan_sent = -1;	/* -1 none pending, else next line index */
static int scan_n;
static struct net_link_ap scan_tab[NET_LINK_SCAN_MAX];

/* What the last wifi_state said, so a change is noticed once. */
/* -2: an override was just sent, so the real state is still owed. */
static int last_st = 0, last_why = 0, last_link = 'u';
/* (0, 0, 'u': what the boot join used to set. The boot join itself is the
 * network thread's now, wl_server.c / net_link_work, gated on the self-test.) */

/* A plain memset of a buffer that is never read again can be optimised
 * away; these held a password and the pairing key. */
static void wipe(void *p, size_t n)
{
	volatile uint8_t *v = p;

	while (n--) {
		*v++ = 0;
	}
}

static void wifi_cand_drop(void)
{
	wipe(&cand, sizeof(cand));
}

static void wifi_kid(const uint8_t key[CFG_PAIR_KEY_LEN], char out[9])
{
	mbedtls_sha256_context c;
	uint8_t d[32];

	mbedtls_sha256_init(&c);
	mbedtls_sha256_starts(&c, 0);
	mbedtls_sha256_update(&c, key, CFG_PAIR_KEY_LEN);
	mbedtls_sha256_finish(&c, d);
	mbedtls_sha256_free(&c);
	snprintf(out, 9, "%02x%02x%02x%02x", d[0], d[1], d[2], d[3]);
}

/* The state the panel and the daemon should believe, from three facts: a
 * set in flight, whether anything is paired, and what the radio says. An
 * unpaired board is "off" even while a join that Forget cut off winds down. */
static const char *wifi_st_name(int *code)
{
	enum net_link_state s = net_link_state();
	int c = 0;

	if (cand.active && !cand_started()) {
		c = 1;
	} else if (cand.active || cfg_paired()) {
		c = s == NL_JOINING ? 1 : s == NL_UP ? 2 : s == NL_FAILED ? 3 : 0;
	}
	*code = c;
	return c == 1 ? "joining" : c == 2 ? "up" : c == 3 ? "fail" : "off";
}

/* One wifi_state line. `why` overrides the radio's own reason (a request
 * refused before it reached the radio). */
static void wifi_send_state(const char *st_override, const char *why_override,
			    bool bare)
{
	static char buf[WIFI_STATE_BUF];
	uint8_t ssid[32];
	size_t ssid_len = 0;
	char psk[CFG_PSK_MAX];
	uint8_t key[CFG_PAIR_KEY_LEN];
	char kid[9] = "";
	char ip[20] = "";
	int code;
	struct wifi_view v = { 0 };

	v.st = wifi_st_name(&code);
	v.rssi = -127;
	if (bare) {
		/* a refused set: it names nothing of its own */
	} else if (cand.active) {
		memcpy(ssid, cand.ssid, cand.ssid_len);
		ssid_len = cand.ssid_len;
		wifi_kid(cand.key, kid);
	} else if (cfg_get_pairing(ssid, &ssid_len, psk, sizeof(psk), key)) {
		wifi_kid(key, kid);
	}
	wipe(psk, sizeof(psk));
	wipe(key, sizeof(key));
	if (code == 2 && net_link_ip(ip, sizeof(ip))) {
		v.ip = ip;
		v.port = WIFI_PORT;
		v.rssi = net_link_rssi();
	}
	v.link = link_active(k_uptime_get()) == LINK_WIFI ? "wifi" : "usb";
	v.ssid = ssid_len ? ssid : NULL;
	v.ssid_len = ssid_len;
	v.kid = kid;
	v.why = code == 3 ? net_link_why_text(net_link_why()) : "";
	if (st_override) {
		v.st = st_override;
	}
	if (why_override) {
		v.why = why_override;
	}
	if (wifi_fmt_state(buf, sizeof(buf), &v) > 0) {
		emit(buf);
	}
	last_st = (st_override || why_override) ? -2 : code;
	last_why = (int)net_link_why();
	last_link = v.link[0];
}

static void wifi_do_set(const char *json)
{
	char s[WIFI_S_BUF], p[WIFI_P_BUF], k[WIFI_K_BUF];
	uint8_t ssid[32], key[CFG_PAIR_KEY_LEN], pw[64];
	char psk[CFG_PSK_MAX] = "";
	int sl = -1, pl = -1, kl = -1;
	bool ok = false;

	/* Parsed into locals: a malformed set must not cancel a good one
	 * already in flight. */
	if (msg_get_str(json, "s", s, sizeof(s)) &&
	    msg_get_str(json, "p", p, sizeof(p)) &&
	    msg_get_str(json, "k", k, sizeof(k))) {
		sl = wifi_hex_decode(s, ssid, sizeof(ssid));
		pl = wifi_hex_decode(p, pw, sizeof(pw));
		kl = wifi_hex_decode(k, key, sizeof(key));
		if (pl > 0) {
			memcpy(psk, pw, (size_t)pl);
		}
		/* cfg_set_pairing refuses a 0x00 in the name, and by then the
		 * join has run: refuse it now. */
		ok = sl > 0 && kl == CFG_PAIR_KEY_LEN && pl > 0 &&
		     memchr(ssid, 0, (size_t)sl) == NULL &&
		     wifi_psk_valid(psk, (size_t)pl);
	}
	wipe(s, sizeof(s));
	wipe(p, sizeof(p));
	wipe(k, sizeof(k));
	wipe(pw, sizeof(pw));
	/* The first reply to every set is `joining`, even one refused below:
	 * the PC counts a `fail` only after it (pc/bridge.py). */
	if (!ok) {
		wifi_send_state("joining", "", true);
		wifi_send_state("fail", WIFI_WHY_BAD_INPUT, true);
		goto out;
	}
	wifi_cand_drop();	/* a newer set replaces an older one */
	memcpy(cand.ssid, ssid, (size_t)sl);
	cand.ssid_len = (size_t)sl;
	memcpy(cand.psk, psk, (size_t)pl);
	memcpy(cand.key, key, sizeof(key));
	cand.active = true;
	cand.req_deadline = k_uptime_get() + WIFI_REQ_DEADLINE_MS;
	wifi_send_state("joining", "", false);	/* the candidate's name and kid */
	cand.ticket = net_link_request_join(cand.ssid, cand.ssid_len, cand.psk);
	if (cand.ticket == 0) {		/* no radio, or refused: say so now */
		wifi_cand_drop();
		wifi_send_state("fail", net_link_why_text(NL_WHY_NOT_FOUND), false);
	}
out:
	wipe(ssid, sizeof(ssid));
	wipe(key, sizeof(key));
	wipe(psk, sizeof(psk));
}

/* A failed candidate on a paired board: the old network was taken down for
 * the attempt, so go back to it rather than sit off until a reboot. */
static void wifi_rejoin_stored(void)
{
	uint8_t ssid[32];
	size_t ssid_len = 0;
	char psk[CFG_PSK_MAX];
	uint8_t key[CFG_PAIR_KEY_LEN];

	if (cfg_get_pairing(ssid, &ssid_len, psk, sizeof(psk), key)) {
		net_link_request_join(ssid, ssid_len, psk);
	}
	wipe(psk, sizeof(psk));
	wipe(key, sizeof(key));
}

/* Main thread only: cand has no lock. Task 2.5's network thread must not
 * call this; it asks main (or net_link_request_down) instead. */
void proto_wifi_cancel(void)
{
	/* The candidate first: a join still running must never persist. */
	wifi_cand_drop();
	net_link_request_down();	/* cheap, and cannot bring the radio up */
}

static void wifi_do_forget(void)
{
	char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];
	/* D28: a leftover SSID from a 1.3.4 build counts, paired or not. */
	bool any = cfg_paired() || cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk));

	wipe(ssid, sizeof(ssid));
	wipe(psk, sizeof(psk));
	/* A join still running must not persist later, and must not leave the
	 * radio associated: the down is queued even when nothing was stored. */
	proto_wifi_cancel();
	if (!any) {
		wifi_send_state(NULL, NULL, false);
		return;
	}
	/* Flash next: the secrets are the urgent part. The radio's down is
	 * queued behind a running join (up to 30 s) and reports on its own. */
	net_link_request_down();
	if (cfg_clear_pairing() == 0) {
		printk("[wifi] forgotten\n");
		wifi_send_state("off", "", false);
	} else {
		printk("[wifi] forget failed\n");
		wifi_send_state("fail", net_link_why_text(NL_WHY_FORGET), false);
	}
}

static void wifi_handle(const char *type, const char *json)
{
	if (strcmp(type, "wifi_scan") == 0) {
		scan_sent = -2;		/* waiting for the radio */
		net_link_request_scan();
	} else if (strcmp(type, "wifi_set") == 0) {
		wifi_do_set(json);
	} else if (strcmp(type, "wifi_forget") == 0) {
		wifi_do_forget();
	}
}

/* Once per proto_service(): stream scan results one line per call (twenty
 * lines at once would flood the deferred log), settle a set in flight, and
 * report any change of state. */
static void wifi_poll(void)
{
	char buf[WIFI_NET_BUF];

	if (scan_sent == -2) {
		scan_n = net_link_scan_results(scan_tab, NET_LINK_SCAN_MAX);
		if (scan_n != -EAGAIN) {
			scan_n = scan_n < 0 ? 0 : scan_n;
			scan_sent = 0;
		}
	} else if (scan_sent >= 0) {
		/* An empty scan still ends visibly: i=0, n=0, no network. */
		bool empty = scan_n == 0;

		if (wifi_fmt_net(buf, sizeof(buf), scan_sent, scan_n,
				 empty ? NULL : &scan_tab[scan_sent]) > 0) {
			emit(buf);
		}
		scan_sent++;
		if (empty || scan_sent >= scan_n) {
			scan_sent = -1;
		}
	}

	if (cand_started()) {
		enum net_link_state st = net_link_state();

		if (st == NL_UP) {
			int rc = cfg_set_pairing(cand.ssid, cand.ssid_len,
						 cand.psk, cand.key);

			wifi_cand_drop();
			if (rc == 0) {
				printk("[wifi] paired\n");
				link_verify_window(k_uptime_get());	/* D65 */
				wifi_send_state(NULL, NULL, false);
			} else {
				wifi_send_state("fail", WIFI_WHY_SAVE, false);
				if (cfg_paired()) {
					wifi_rejoin_stored();
				} else {
					net_link_request_down();
				}
			}
		} else if (st == NL_FAILED) {
			/* Explicit: with no candidate and nothing paired the
			 * derived state would read "off", and the PC would
			 * never see the fail it is waiting for. */
			wifi_cand_drop();
			wifi_send_state("fail", net_link_why_text(net_link_why()),
					false);
			if (cfg_paired()) {
				wifi_rejoin_stored();
			}
		}
	}
	if (cand.active && k_uptime_get() >
	    (cand.started ? cand.deadline : cand.req_deadline)) {
		wifi_cand_drop();
		wifi_send_state("fail", net_link_why_text(NL_WHY_NOT_FOUND), false);
		if (cfg_paired()) {
			wifi_rejoin_stored();
		}
	}

	{
		int code;
		char l;

		(void)wifi_st_name(&code);
		l = link_active(k_uptime_get()) == LINK_WIFI ? 'w' : 'u';
		if (wifi_state_changed(last_st, last_why, last_link, code,
				       (int)net_link_why(), l)) {
			wifi_send_state(NULL, NULL, false);
		}
	}
}

static void dispatch(const char *json, enum proto_src src)
{
	char type[16];
	if (!msg_get_str(json, "t", type, sizeof(type))) {
		return; /* not a protocol line */
	}

#ifdef CONFIG_BLINK_TOUCH_INJECT
	/* Bench images only: the PC's finger, and its question about what
	 * the panel shows (touch_inject.h). Dispatched BEFORE the host stamp
	 * and link_usb_heard(): these lines do not count as a USB host, so a
	 * live WiFi session and an OTA in flight survive board_touch.py
	 * --touch-only (D55). */
	if (touch_inject_dispatch(type, json)) {
		return;
	}
#endif
	last_host_ms = k_uptime_get();
	/* A line off the WiFi session is not a USB host: counting it would put
	 * the link back on USB and close the session that carried it. */
	if (src == PROTO_SRC_USB) {
		link_usb_heard(last_host_ms);
	}
	host_seen = true;
	host_lost = false;
	if (strcmp(type, "bye") == 0) {
		/*
		 * A clean exit -- uninstall. Without this a computer with no
		 * app any more looks exactly like a sleeping one, and the
		 * face would doze instead of saying "connecting".
		 */
		printk("[proto] host says bye\n");
		host_bye = true;
		/* The daemon is going, and its builder with it (Launcher.close
		 * in pc/theme_server.py). */
		ui_settings_theme_edit_host_gone();
		return;
	}
	host_bye = false;
	if (strcmp(type, "usage") == 0) {
		double sp = 0, wp = 0;
		/* Remaining seconds, not the absolute resets_at timestamps: the
		 * board has no wall clock when tethered over USB, so the daemon
		 * does the subtraction. -1 means unknown.
		 */
		double ss = -1, ws = -1;

		num(json, "session_pct", &sp, -1, PCT_MAX);
		num(json, "weekly_pct", &wp, -1, PCT_MAX);
		num(json, "session_resets_in_s", &ss, -1, SECS_MAX);
		num(json, "weekly_resets_in_s", &ws, -1, SECS_MAX);
		usage_view_update(sp, (int32_t)ss, wp, (int32_t)ws);

		/* Flat model key (protocol.py flattens its models list for
		 * us); an absent key leaves the -1 "unknown" default. */
		double mf = -1;

		num(json, "fable_pct", &mf, -1, PCT_MAX);
		usage_view_set_models(mf);

		/* Every usage message says whether its own numbers can be
		 * trusted, so the dot is decided here rather than by a
		 * separate status message arriving afterwards.
		 *
		 * This must run AFTER usage_view_update() and
		 * usage_view_set_models(), both of which set OK internally --
		 * ordering it earlier would have the amber immediately
		 * overwritten by green.
		 *
		 * Before this existed the daemon had to fake it, sending
		 * status "rate_limited" purely because that string already
		 * mapped to amber -- which made a stale reading and a real
		 * rate limit indistinguishable on the panel. An absent key
		 * leaves `stale` false, so a daemon older than this firmware
		 * still reads as OK rather than as a warning.
		 */
		bool stale = false;

		msg_get_bool(json, "stale", &stale);
		usage_view_set_provider1_stale(stale);
		/*
		 * The status is armed if EITHER page is old; usage_view then
		 * shows the warning only on a page it is actually true of.
		 * Set below, after p2 has been parsed, because until then only
		 * half the answer is known -- and arming on the first
		 * provider alone was the mirror of the bug this fixes: a fresh
		 * codex reading beside a stale claude one left the claude page
		 * claiming to be current.
		 */

		/*
		 * The multi-provider fields. All OPTIONAL, and all defaulting
		 * to "say nothing": the daemon omits a key it has no answer
		 * for rather than sending a sentinel, so an absent key and an
		 * old daemon are the same case here and neither may turn an
		 * indicator on. See pc/protocol.usage().
		 */
		char state[16];
		enum usage_activity act = USAGE_ACTIVITY_NONE;

		if (msg_get_str(json, "state", state, sizeof(state))) {
			act = usage_activity_from_state(state);
		}
		usage_view_set_activity(act);

		/* Session and agent counts. Absent means zero here rather than
		 * unknown -- a daemon that sends no count is one with nothing
		 * to report, and the readout hides itself at one session with
		 * no agents anyway. */
		double ns = 0, na = 0;

		num(json, "n_sess", &ns, 0, 9999);
		num(json, "n_agents", &na, 0, 9999);
		usage_view_set_sessions((int)ns, (int)na);

		double nr = 0, nw = 0, nst = 0;

		/* Sent since the counts existed, dropped on the floor until
		 * the pip row had a use for them. Absent means zero on both
		 * sides, which is why the daemon omits them. */
		num(json, "n_run", &nr, 0, 9999);
		num(json, "n_wait", &nw, 0, 9999);
		num(json, "n_stuck", &nst, 0, 9999);
		usage_view_set_counts((int)ns, (int)nr, (int)nw, (int)nst);

		char p1[16];

		if (msg_get_str(json, "provider", p1, sizeof(p1))) {
			usage_view_set_provider1(p1);
		}

		/*
		 * How fast the session window is filling, when nothing can say
		 * WHEN it rolls. Absent is the normal case and means zero, so a
		 * daemon older than this firmware simply never fills the
		 * countdown slot -- which is what it does today.
		 *
		 * Sent only when there is no session reset time; the two are
		 * mutually exclusive on the daemon's side, so this is set
		 * unconditionally and the view prefers the countdown anyway.
		 */
		double burn = 0;

		num(json, "burn_pph", &burn, 0, 9999);
		usage_view_set_burn(burn);

		char p2[16];
		double p2s = -1, p2w = -1, p2si = -1, p2wi = -1;

		if (msg_get_str(json, "p2", p2, sizeof(p2))) {
			bool p2stale = false;

			num(json, "p2_session_pct", &p2s, -1, PCT_MAX);
			num(json, "p2_weekly_pct", &p2w, -1, PCT_MAX);
			num(json, "p2_s_in_s", &p2si, -1, SECS_MAX);
			num(json, "p2_w_in_s", &p2wi, -1, SECS_MAX);
			/* Absent leaves this false, so a daemon older than
			 * this firmware reports its second provider as fresh
			 * rather than as old -- the reading it sent IS the
			 * latest one it has, and the alternative is a page
			 * permanently labelled stale by a missing key. */
			msg_get_bool(json, "p2_stale", &p2stale);
			usage_view_set_provider2(p2, p2s, p2w, (int32_t)p2si,
						 (int32_t)p2wi, p2stale);
			stale = stale || p2stale;
		} else {
			usage_view_set_provider2("", -1, -1, -1, -1, false);
		}

		/*
		 * How old each READING is, as opposed to how long ago the
		 * daemon last spoke. Both defaults stay -1 when the keys are
		 * absent, which is how a daemon older than this firmware
		 * leaves the panel counting from the message exactly as it
		 * always did.
		 *
		 * After the p2 block: set_provider2() with an empty tag drops
		 * that page, and an age written before it would be an age for
		 * a page that no longer exists.
		 */
		double age = -1, p2age = -1, active_age = -1;

		num(json, "age_s", &age, -1, SECS_MAX);
		num(json, "p2_age_s", &p2age, -1, SECS_MAX);
		usage_view_set_ages((int32_t)age, (int32_t)p2age);

		/*
		 * And the other freshness question, which is not drawn
		 * anywhere: how long since ANY tool on that machine wrote
		 * anything at all. One figure for the whole desk rather than
		 * one per page, because it decides whether the board dozes
		 * and the board dozes as one screen.
		 *
		 * Absent leaves it -1, and usage_freshness_note then reads
		 * the reading's own age instead -- which is exactly right for
		 * a daemon that predates the field. See usage_freshness.h.
		 */
		num(json, "active_age_s", &active_age, -1, SECS_MAX);

		/* The same facts, kept where main.c can ask and a laptop can
		 * test the reasoning about them. See usage_freshness.h. */
		usage_freshness_note((int32_t)age, (int32_t)active_age, act,
				     k_uptime_get());

		/*
		 * AFTER both providers, and after the calls above that set OK
		 * internally -- usage_view_update() and set_models() both do,
		 * so arming this earlier would have the amber immediately
		 * overwritten by green. See the note where `stale` is read.
		 */
		if (stale) {
			usage_view_set_status(USAGE_STATUS_STALE);
		}

		printk("[usage] session %.0f%% (%ds)  weekly %.0f%% (%ds)%s%s\n",
		       sp, (int)ss, wp, (int)ws, stale ? "  STALE" : "",
		       act == USAGE_ACTIVITY_NONE ? "" : " +state");
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
	} else if (strcmp(type, "edition") == 0) {
		/*
		 * A factory fact arriving over the cable, ONCE, after the
		 * board is programmed -- see cfg_edition in cfg_store.h. The
		 * enclosure decides which clip is right, and a user who could
		 * flip it would only be putting the wrong animation in the
		 * wrong box.
		 *
		 * "Not reachable from the settings screen" is not the whole
		 * story and used to be treated as if it were. This message
		 * arrives from whatever is on the other end of the cable, and
		 * the tool that sends it is the same binary the user installs
		 * -- so the enforcement has to be in the record, not in the
		 * UI. cfg_set_edition latches on the first successful write
		 * and refuses every one after it.
		 *
		 * Takes effect on the next boot, because what it selects is a
		 * boot animation. Saying so in the log is the difference
		 * between "it did nothing" and "it will do it in a moment".
		 */
		char ed[12];

		if (msg_get_str(json, "edition", ed, sizeof(ed))) {
			uint8_t v;

			if (strcmp(ed, "claude") == 0) {
				v = CFG_EDITION_CLAUDE;
			} else if (strcmp(ed, "codex") == 0) {
				v = CFG_EDITION_CODEX;
			} else {
				printk("[cfg] unknown edition '%s'; ignored\n", ed);
				return;
			}
			/*
			 * The LATCH decides, not the value.
			 *
			 * An earlier version skipped the write whenever the
			 * stored edition already matched, which looks like a
			 * harmless optimisation and is a hole: 0 means both
			 * "Claude" and "never stamped", so provisioning a
			 * blank board as claude reported success, wrote
			 * nothing, and left it stampable as codex afterwards
			 * by anyone with the cable.
			 */
			int rc;

			if (cfg_edition_locked()) {
				printk("[cfg] edition already stamped as %s;"
				       " refusing to change it\n",
				       cfg_get_edition() == CFG_EDITION_CODEX
					       ? "codex" : "claude");
			} else if ((rc = cfg_set_edition(v)) == 0) {
				printk("[cfg] edition set to %s"
				       " (applies on next boot)\n", ed);
			} else {
				printk("[cfg] could not store the edition"
				       " (%d)\n", rc);
			}
		}
	} else if (strcmp(type, "welcome") == 0) {
		double hv = 0;

		/*
		 * Both of these used to be dropped on the floor. The board had
		 * no way to tell a customer that the half of the product on
		 * their computer was the half that needed updating -- and that
		 * is the only half that cannot update itself from here.
		 */
		if (!msg_get_str(json, "app_ver", host_ver, sizeof(host_ver))) {
			host_ver[0] = '\0';
		}
		host_proto = msg_get_double(json, "v", &hv) ? (int)hv : 0;
		/* Every welcome decides it afresh: an app downgraded to one
		 * without the builder must stop being offered as having one. */
		host_can_theme = theme_caps_has(json, "theme");
		/* And whether "Edit on computer" is held: this daemon's caps
		 * say whether it reports a builder's end, and greet()'s
		 * welcome whether one it opened for us is open right now --
		 * the only way a board that rebooted, or was unplugged, while
		 * one opened can know (edit_hold.h). */
		ui_settings_theme_edit_welcome(json);
		printk("[proto] host connected: app %s, protocol %d\n",
		       host_ver[0] ? host_ver : "?", host_proto);
		/*
		 * Every host connection is a firmware check, not just the
		 * first after boot. The daemon replaces itself for an app
		 * update and comes back with a newer release on its feed;
		 * a board that only asked once per boot sat on 1.0.0 with
		 * 1.1.1 published (2026-08-29). The daemon now offers on
		 * its own as well, for boards running this code's
		 * predecessors; this side covers the case where it does not
		 * know what we run.
		 */
		ota_request_check();
		/*
		 * Answer. The daemon sends `welcome` as a question -- "is a
		 * Blink board on this port?" -- and decides whether to pull
		 * the reset line on whether anything comes back within 1.5 s
		 * (claude_usage_bridge.probe_is_our_board). This handler used
		 * to reply with nothing: the ota_query it was credited with
		 * is sent once per boot by main.c, so from the second host
		 * connection of a boot onwards the probe heard silence unless
		 * a 10 s ping happened to land, and the board was reset -- and
		 * replayed its boot clip -- on most daemon restarts anyway.
		 * The preference is the right answer: it is what a host that
		 * has just connected needs to know, and it already rides with
		 * hello for the same reason.
		 */
		proto_send_pref();
		/* And the theme, for the same reason as the preference: a
		 * daemon that restarted without resetting the board heard
		 * the hello that carried it in a previous life. */
		proto_send_theme_now("boot");
		wifi_send_state(NULL, NULL, false);
	} else if (strncmp(type, "wifi_", 5) == 0) {
		if (src == PROTO_SRC_USB) {	/* dropped without a reply otherwise */
			wifi_handle(type, json);
		}
	} else if (strcmp(type, "theme") == 0) {
		/*
		 * Validated whole before anything is applied: a line with one
		 * bad value is refused entire, and the board keeps the theme
		 * it has (contract 2026-10-27). Nothing from the line is
		 * logged -- only that it was refused.
		 */
		struct theme_msg m;

		if (!theme_msg_parse(json, &m)) {
			printk("[theme] line refused\n");
			return;
		}
		/* A preview or save is the builder at work, which answers an
		 * "Edit on computer" that is still waiting as well as an
		 * explicit reply would, and keeps a held slot held. Not an
		 * explicit ok: the builder may be one `blink theme` opened,
		 * whose end the board is never told. */
		if (m.op != THEME_OP_CANCEL) {
			ui_settings_theme_edit_builder_seen();
		}
		ui_theme_wire(&m);
	} else if (strcmp(type, "theme_edit") == 0) {
		/* The daemon's answer to ours: ok 1 once a browser is open,
		 * ok 0 when it has no display to open one on. Absent reads as
		 * 0, the answer that tells the user what to do instead.
		 * done 1 is the other line it sends (Addendum 6): the builder
		 * it opened has ended, so the next tap must ask again. */
		double ok = 0, done = 0;

		msg_get_double(json, "done", &done);
		if (done == 1) {
			ui_settings_theme_edit_done();
			return;
		}
		msg_get_double(json, "ok", &ok);
		ui_settings_theme_edit_reply(ok == 1);
	} else if (strcmp(type, "edit_state") == 0) {
		/* The daemon restating whether the builder it opened for "Edit
		 * on computer" is open (pc/protocol.edit_state): every 60 s,
		 * and at once when it hears this board after a silence, so a
		 * hold freed on HOST LOST comes back on a connection that never
		 * closed. Absent reads as 0: a line that says nothing must not
		 * keep a button away. */
		double editing = 0;

		msg_get_double(json, "editing", &editing);
		ui_settings_theme_edit_state(editing == 1);
	} else if (strcmp(type, "ota_avail") == 0) {
		double sz = 0;

		if (msg_get_str(json, "version", ota_m.version,
				sizeof(ota_m.version)) &&
		    msg_get_str(json, "sha256", ota_m.sha256,
				sizeof(ota_m.sha256)) &&
		    msg_get_double(json, "size", &sz) && sz > 0) {
			ota_m.size = (uint32_t)sz;
			/* Optional: absent on any release that predates pair
			 * updates, and on any release where this computer's
			 * app is already current. */
			if (!msg_get_str(json, "app", ota_app,
					 sizeof(ota_app))) {
				ota_app[0] = '\0';
			}
			ota_staged = true;
			ota_ui_set(OTA_UI_AVAILABLE, &ota_m, 0);
			printk("[proto] daemon offers %s (%u bytes)\n",
			       ota_m.version, ota_m.size);
		} else {
			ota_ui_set(OTA_UI_FAILED, NULL, 0);
		}
	} else if (strcmp(type, "ota_begin") == 0) {
		/* The write is starting; nothing can now come between this and
		 * esptool resetting us, so the next boot's report is honest
		 * whichever way it goes. */
		char v[16];

#if defined(CONFIG_BLINK_WIFI_LINK)
		if (src == PROTO_SRC_WIFI) {
			/* The board writes slot 1 itself; no breadcrumb yet. */
			ota_wifi_begin(json);
		} else
#endif
		if (msg_get_str(json, "version", v, sizeof(v))) {
			char trail[CFG_OTA_VER_MAX];

			printk("[proto] ota: writing %s\n", v);
			/* Where we came FROM as well as where we are going.
			 * The next boot's popup needs it to say what changed
			 * across a jump of more than one release, and this is
			 * the last moment anything knows: after the write
			 * this image is gone. See whatsnew.h for why it is
			 * packed into the existing field. */
			whatsnew_trail(BLINK_FW_VERSION, v, trail,
				       sizeof(trail));
			cfg_set_ota_state(1, trail);
		}
#if defined(CONFIG_BLINK_WIFI_LINK)
	} else if (strcmp(type, "ota_end") == 0 && src == PROTO_SRC_WIFI) {
		ota_wifi_end();
#endif
	} else if (strcmp(type, "ota_resume") == 0) {
		/*
		 * The daemon replaced itself mid-update and is picking up an
		 * install this board already approved. Without this the panel
		 * would come back to an "Install?" prompt for something
		 * already under way, and the user would answer it twice.
		 */
		if (msg_get_str(json, "version", ota_m.version,
				sizeof(ota_m.version))) {
			printk("[proto] daemon resuming install of %s\n",
			       ota_m.version);
			ota_ui_set_source(src == PROTO_SRC_WIFI ? OTA_SRC_WIFI :
					  OTA_SRC_USB);
			ota_ui_set(OTA_UI_DOWNLOADING, &ota_m, 0);
		}
	} else if (strcmp(type, "ota_none") == 0) {
		ota_staged = false;
		/*
		 * Nothing for the BOARD, which says nothing about the app --
		 * and this is the only message that can carry that answer to a
		 * board whose firmware is already current, which is the state
		 * a working desk sits in.
		 *
		 * Absent means the daemon predates the field, and absent has
		 * to read as "nobody told me" rather than "there is none":
		 * proto_host_outdated() then says no, the row goes blank, and
		 * blank is the honest answer to a question nothing answered.
		 */
		if (!msg_get_str(json, "app", ota_app, sizeof(ota_app))) {
			ota_app[0] = '\0';
		}
		ota_ui_set(OTA_UI_UP_TO_DATE, NULL, 0);
	} else if (strcmp(type, "ota_error") == 0) {
		char why[48] = "";

#if defined(CONFIG_BLINK_WIFI_LINK)
		/* The daemon gave up on a WiFi stream: drop the flash context. */
		ota_net_abort();
#endif
		ota_staged = false;
		ota_ui_set(OTA_UI_FAILED, NULL, 0);
		/* After the state, not before: ota_ui_set() clears the reason
		 * on any state that is not FAILED, so the order matters. */
		if (msg_get_str(json, "why", why, sizeof(why))) {
			printk("[proto] ota failed: %s\n", why);
			ota_ui_set_error(why);
		}
	} else if (strcmp(type, "status") == 0) {
		char st[24] = "";

		msg_get_str(json, "state", st, sizeof(st));
		printk("[proto] host status: %s\n", st);
		usage_view_set_status(strcmp(st, "rate_limited") == 0 ?
				      USAGE_STATUS_STALE : USAGE_STATUS_ERROR);
	} else if (strcmp(type, "session") == 0) {
		/*
		 * Which project is doing the thing the dot is already
		 * colouring. Its own message rather than a field on `usage`
		 * because that line is at 506 of the 512 this parser accepts
		 * and an over-long one is dropped whole -- see
		 * pc/protocol.py:331.
		 *
		 * An absent label is the normal several-sessions case, not a
		 * parse failure: the daemon refuses to name one of several,
		 * and `n` carries the meaning instead.
		 */
		char lbl[28] = "";
		double n = 0;

		msg_get_str(json, "label", lbl, sizeof(lbl));
		num(json, "n", &n, 0, 9999);
		usage_view_set_session(lbl, (int)n);
	} else if (strcmp(type, "svc") == 0) {
		/*
		 * What the provider's public status page says. The contract is
		 * pc/protocol.py svc(); the decisions -- which levels exist,
		 * how long one stands without being re-stated, what the line
		 * says -- are in svc_state.c, where a host test reaches them.
		 *
		 * `level` gets 16 bytes for a word of at most 8: msg_get_str
		 * truncates rather than failing, and a newer daemon's
		 * "majority" cut to the 5 bytes of a tighter buffer would read
		 * as "major". At 16 nothing truncates to a word this firmware
		 * knows. `what` is absent at ok, which leaves it "".
		 */
		char prov[12] = "", lvl[16] = "", what[SVC_WHAT_MAX + 4] = "";

		if (msg_get_str(json, "provider", prov, sizeof(prov))) {
			msg_get_str(json, "level", lvl, sizeof(lvl));
			msg_get_str(json, "what", what, sizeof(what));
			svc_note(prov, lvl, what, k_uptime_get());
			usage_view_svc_changed();
		}
	}
	/* unknown types ignored */
}

static void drain_rx(void)
{
	uint8_t c;
	/*
	 * Set on overflow and cleared at the next newline. Without it an
	 * over-long line was not dropped whole, as the comment on LINE_MAX
	 * (and pc/protocol.py, which mirrors it) claimed: line_len went back
	 * to 0 and the REST of the same line went on accumulating, to be
	 * dispatched as a message of its own. With a scanner that finds keys
	 * anywhere in the text, a tail that happened to contain "t":"..."
	 * parsed as a frame the host never sent.
	 */
	static bool discarding;

	while (ring_buf_get(&rx_ring, &c, 1) == 1) {
		if (c == '\n' || c == '\r') {
			if (discarding) {
				discarding = false;
			} else if (line_len > 0) {
				line[line_len] = '\0';
				dispatch(line, PROTO_SRC_USB);
				/* A wifi_set line carried a password and a key. */
				wipe(line, sizeof(line));
			}
			line_len = 0;
		} else if (discarding) {
			/* the tail of a line already given up on */
		} else if (line_len < LINE_MAX - 1) {
			line[line_len++] = (char)c;
		} else {
			wipe(line, sizeof(line));	/* may hold a wifi_set */
			line_len = 0;
			discarding = true; /* overflow: drop the WHOLE line */
		}
	}

	if (rx_dropped) {
		printk("[proto] rx ring overflow, dropped %u bytes\n", rx_dropped);
		rx_dropped = 0;
	}
}

#if defined(CONFIG_BLINK_WIFI_LINK)
/*
 * The WiFi session's frames, in arrival order (D34). An L frame is one line
 * and meets the same over-long rule as the UART: a line that does not fit
 * LINE_MAX is dropped whole, never split. O and G frames go to the sinks
 * Tasks 5.1 and 6.3 register; with none, the session is closed, since
 * nothing may be half-applied.
 */
static void drain_wifi(void)
{
	static uint8_t body[WL_PT_MAX];
	uint8_t type;
	int n;

	/* Bounded, so a flooding peer cannot hold the main thread. */
	for (int i = 0; i < WL_RX_FRAMES; i++) {
		n = wl_server_rx_frame(&type, body, sizeof(body));
		if (n == -EAGAIN) {
			return;
		}
		if (n < 0) {
			continue;
		}
		if (type != WL_T_LINE) {
			wl_server_sink(type, body, (size_t)n);
			continue;
		}
		/* One line per frame: a newline anywhere but the end is two
		 * lines in one, refused whole. */
		if (n > 1 && memchr(body, '\n', (size_t)n - 1) != NULL) {
			continue;
		}
		while (n > 0 && (body[n - 1] == '\n' || body[n - 1] == '\r')) {
			n--;
		}
		if (n > 0 && n < LINE_MAX) {
			/* In place: line[] belongs to the UART, which may be
			 * halfway through a line. body has 517 bytes, n < 512. */
			body[n] = '\0';
			dispatch((const char *)body, PROTO_SRC_WIFI);
		}
	}
}
#endif

void proto_init(void)
{
	line_len = 0;

	if (console_dev && device_is_ready(console_dev)) {
		uart_irq_callback_user_data_set(console_dev, uart_isr, NULL);
		uart_irq_rx_enable(console_dev);
	}

#ifdef CONFIG_BLINK_TOUCH_INJECT
	touch_inject_init();
#endif
#if defined(CONFIG_BLINK_WIFI_LINK)
	wl_server_set_sink(WL_T_OTA, ota_o_sink);
#endif
	send_hello();
	last_ping_ms = k_uptime_get();
}

const char *proto_host_version(void)
{
	return host_ver;
}

bool proto_host_outdated(void)
{
	/*
	 * What the DAEMON said, not what this board worked out.
	 *
	 * This used to be `ota_version_newer(BLINK_FW_VERSION, host_ver)` -- a
	 * comparison between our own firmware and the app's version. That
	 * answers "is the app older than ME", and the row it feeds says "App
	 * is old", which is a claim about the RELEASE. The two agree only
	 * while both halves ship from one tag, and any board running ahead of
	 * the published feed breaks it: every developer's desk from the first
	 * local build onwards, and any unit hand-flashed from an unreleased
	 * tree. Seen 2026-09-11 on a board calling the newest published app
	 * old, while the daemon logged "release has 1.3.2 -- nothing to do"
	 * on every query.
	 *
	 * The daemon is the only party that can answer it, because it is the
	 * one that fetches and verifies the signed manifest, and it now says
	 * so on ota_avail and ota_none alike. ota_app holds that answer and
	 * proto_ota_check() clears it, so a stale yes cannot outlive the
	 * question.
	 *
	 * Still only ever an advisory: usage keeps flowing either way, and a
	 * daemon too old to send the field leaves this false, which is the
	 * right way to be wrong.
	 */
	return ota_app[0] != '\0';
}

bool proto_host_seen(void)
{
	return host_seen;
}

void proto_service(void)
{
	drain_rx();
	wifi_poll();
#if defined(CONFIG_BLINK_WIFI_LINK)
	ota_wifi_service();
	drain_wifi();
#endif
#ifdef CONFIG_BLINK_TOUCH_INJECT
	touch_inject_service();
#endif
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
		printk("[proto] host went away%s\n",
		       host_bye ? " (said bye)" : "");
		usage_view_set_status(USAGE_STATUS_DISCONNECTED);
		host_seen = false;
		host_lost = !host_bye;
		host_bye = false;
		ui_settings_theme_edit_host_gone();
	}
}

void proto_resync(void)
{
	send_hello();
}

bool proto_host_lost(void)
{
	return host_lost;
}

uint8_t proto_link_kind(void)
{
	return (uint8_t)link_active(k_uptime_get());
}
