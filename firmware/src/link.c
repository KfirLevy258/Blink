#include "link.h"

#ifdef __ZEPHYR__
#include <errno.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#if defined(CONFIG_BLINK_WIFI_LINK)
/* Task 2.5. Returns -ENOSPC when the TX ring is full. Declared only when the
 * WiFi link is built, so an image without it still links. */
int wl_server_tx_line(const char *line, int timeout_ms);
#endif
#endif

#define VERIFY_WINDOW_MS 60000
#define BACKOFF_MAX_S 60

static bool usb_ever;
static int64_t usb_ms;
static bool wifi_is_up;
static bool verify_open;
static int64_t verify_until_ms;
static int fails;
static int64_t backoff_until_ms;

void link_usb_heard(int64_t now_ms)
{
	usb_ever = true;
	usb_ms = now_ms;
}

void link_wifi_up(bool up)
{
	wifi_is_up = up;
}

static bool usb_active(int64_t now_ms)
{
	return usb_ever && (now_ms - usb_ms) <= LINK_HOST_TIMEOUT_MS;
}

enum link_kind link_active(int64_t now_ms)
{
	if (usb_active(now_ms)) {
		return LINK_USB;
	}
	return wifi_is_up ? LINK_WIFI : LINK_NONE;
}

enum link_accept link_wifi_may_accept(int64_t now_ms)
{
	if (now_ms < backoff_until_ms) {
		return LINK_ACCEPT_NO;
	}
	if (!usb_active(now_ms)) {
		return LINK_ACCEPT_SESSION;
	}
	if (verify_open && now_ms <= verify_until_ms) {
		return LINK_ACCEPT_VERIFY_ONLY;
	}
	return LINK_ACCEPT_NO;
}

void link_verify_window(int64_t now_ms)
{
	verify_open = true;
	verify_until_ms = now_ms + VERIFY_WINDOW_MS;
}

bool link_wifi_must_close(int64_t now_ms)
{
	/* Deliberately ignores the backoff: it gates new connections only. */
	return wifi_is_up && usb_active(now_ms);
}

void link_auth_failed(int64_t now_ms)
{
	int64_t s = 1;
	/* Cap the exponent: 2^6 = 64 is the first to exceed 60, so 7 failures reach the cap, and fails must not
	 * overflow on a board left under attack for months. */
	if (fails < 7) {
		fails++;
	}
	s = (int64_t)1 << (fails - 1);
	if (s > BACKOFF_MAX_S) {
		s = BACKOFF_MAX_S;
	}
	backoff_until_ms = now_ms + s * 1000;
}

void link_auth_ok(void)
{
	fails = 0;
	backoff_until_ms = 0;
	/* One verified session is all the window allows (D65). */
	verify_open = false;
}

bool link_replace_session(bool newcomer_authenticated)
{
	return newcomer_authenticated;
}

#ifdef __ZEPHYR__
/*
 * The only path from proto.c's emit() to a link. Diagnostics
 * (tagged console lines) never come through here, so nothing but protocol
 * lines can reach the network. USB stays a printk: the long prj.conf comment
 * on the deferred log still applies.
 *
 * A full WiFi TX ring closes the session instead of dropping the line. The
 * bench image once lost 22 replies in one run to a lossy log buffer
 * (prj.conf, 25912f7) and neither side said so; a closed session is loud,
 * and the daemon reconnects at once.
 */
void link_emit(const char *line)
{
#if defined(CONFIG_BLINK_WIFI_LINK)
	if (link_active(k_uptime_get()) == LINK_WIFI) {
		if (wl_server_tx_line(line, 200) == -ENOSPC) {
			/* Task 2.5's link_wifi_up(false) path must close the
			 * socket (spec 5.4); this only stops routing to it. */
			link_wifi_up(false);
		}
		return;
	}
#endif
	printk("%s\n", line);
}
#endif
