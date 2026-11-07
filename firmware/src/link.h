#pragma once
/*
 * Which link protocol lines go to (spec 5.3, D13). One place decides it, so
 * proto.c, the network thread and the UI cannot disagree. The decision is a
 * pure function of a few timestamps; callers pass the clock in, which is what
 * lets tests/link/host_test.c run it on a laptop.
 *
 * Threading: the state is written by two threads (main via link_usb_heard,
 * link_verify_window, link_emit; the network thread via the rest). Every call
 * takes one spinlock in link.c, because the int64_t fields tear on Xtensa.
 */
#include <stdbool.h>
#include <stdint.h>

enum link_kind { LINK_NONE, LINK_USB, LINK_WIFI };

/* A USB host has spoken within this long => USB owns the link. The one
 * definition (it used to be HOST_TIMEOUT_MS in proto.c): three missed 10 s
 * pings. */
#define LINK_HOST_TIMEOUT_MS 30000

/* A protocol line arrived on the UART (not merely power on the cable). */
void link_usb_heard(int64_t now_ms);
/* The network thread has an authenticated, live session (never a verify one). */
void link_wifi_up(bool up);
bool link_wifi_is_up(void);
/* USB if heard within LINK_HOST_TIMEOUT_MS, else WIFI if a session is up. */
enum link_kind link_active(int64_t now_ms);

enum link_accept { LINK_ACCEPT_NO, LINK_ACCEPT_SESSION, LINK_ACCEPT_VERIFY_ONLY };
/* What the listener does with a new connection (spec 5.4). */
enum link_accept link_wifi_may_accept(int64_t now_ms);
/* A successful wifi_set over USB opens the verify window (D65): 60 s, or
 * until one session authenticates, whichever is first. */
void link_verify_window(int64_t now_ms);
/* A live session must go: USB spoke. */
bool link_wifi_must_close(int64_t now_ms);
/* A failed handshake: global backoff min(2^(n-1), 60) s, so 1, 2, 4 ... 60. */
void link_auth_failed(int64_t now_ms);
/* A completed handshake: clears the backoff and closes the verify window. */
void link_auth_ok(void);
/* D29: a live session is replaced only by a newcomer that authenticated;
 * a stranger cannot evict a half-open one. */
bool link_replace_session(bool newcomer_authenticated);

/* Board only: the one writer of protocol lines. */
void link_emit(const char *line);
