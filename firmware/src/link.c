#include "link.h"

#ifdef __ZEPHYR__
#include <errno.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#if defined(CONFIG_BLINK_WIFI_LINK)
/* Task 2.5. Returns -ENOSPC when the TX ring is full. Declared only when the
 * WiFi link is built, so an image without it still links. */
int wl_server_tx_line(const char *line, int timeout_ms);

/* Until Task 2.5's definition replaces it: no session, so nothing to send.
 * Without it an image with BLINK_WIFI_LINK on (the default) did not link. */
__weak int wl_server_tx_line(const char *line, int timeout_ms)
{
	ARG_UNUSED(line);
	ARG_UNUSED(timeout_ms);
	return -ENOTCONN;
}
#endif
#endif

/* Every function below takes this: main writes link_usb_heard and
 * link_verify_window, the network thread the rest, and int64_t tears on
 * Xtensa (32-bit). Nothing under it blocks, so a spinlock is enough. Host
 * tests are one thread and compile it away. */
#ifdef __ZEPHYR__
static struct k_spinlock lk;
#define LOCK() k_spinlock_key_t lkey = k_spin_lock(&lk)
#define UNLOCK() k_spin_unlock(&lk, lkey)
#else
#define LOCK() (void)0
#define UNLOCK() (void)0
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
	LOCK();
	usb_ever = true;
	usb_ms = now_ms;
	UNLOCK();
}

void link_wifi_up(bool up)
{
	LOCK();
	wifi_is_up = up;
	UNLOCK();
}

bool link_wifi_is_up(void)
{
	LOCK();
	bool r = wifi_is_up;

	UNLOCK();
	return r;
}

static bool usb_active(int64_t now_ms)
{
	return usb_ever && (now_ms - usb_ms) <= LINK_HOST_TIMEOUT_MS;
}

enum link_kind link_active(int64_t now_ms)
{
	LOCK();
	enum link_kind k = usb_active(now_ms) ? LINK_USB :
			   wifi_is_up ? LINK_WIFI : LINK_NONE;

	UNLOCK();
	return k;
}

static enum link_accept may_accept(int64_t now_ms)
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

enum link_accept link_wifi_may_accept(int64_t now_ms)
{
	LOCK();
	enum link_accept a = may_accept(now_ms);

	UNLOCK();
	return a;
}

void link_verify_window(int64_t now_ms)
{
	LOCK();
	verify_open = true;
	verify_until_ms = now_ms + VERIFY_WINDOW_MS;
	UNLOCK();
}

bool link_wifi_must_close(int64_t now_ms)
{
	/* Deliberately ignores the backoff: it gates new connections only. */
	LOCK();
	bool r = wifi_is_up && usb_active(now_ms);

	UNLOCK();
	return r;
}

void link_auth_failed(int64_t now_ms)
{
	int64_t s = 1;
	LOCK();
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
	UNLOCK();
}

void link_auth_ok(void)
{
	LOCK();
	fails = 0;
	backoff_until_ms = 0;
	/* One verified session is all the window allows (D65). */
	verify_open = false;
	UNLOCK();
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
			/* wl_server's tick sees link_wifi_is_up() go false
			 * and closes the socket (spec 5.4). */
			link_wifi_up(false);
		}
		return;
	}
#endif
	printk("%s\n", line);
}
#endif
