#ifndef WL_SERVER_H
#define WL_SERVER_H

/*
 * The board's network thread (spec S5.4 to S5.6): the one owner of every
 * socket, of the radio's work (net_link_work) and of the two rings between
 * it and the main thread. The main thread never calls send() or recv().
 *
 * Threading: everything below is main-thread-safe except wl_server_close_all,
 * which closes sockets and is for code already running on the network thread
 * (net_link_work's down and join paths).
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/types.h>

#define WL_RX_FRAMES 4		/* Task 0.3's numbers */
#define WL_TX_LINES 8

/* Idempotent. Spawns the thread; -EPERM after a failed self-test (D24). */
int wl_server_start(void);
/* Closes only the live session, "[wifi] session closed" (Forget, a stream
 * with no sink). The daemon reconnects at once. */
void wl_server_close_session(void);
/* Network thread only: every socket, session and candidate (D46 step 1). */
void wl_server_close_all(void);

/* Main thread: the next frame in arrival order (L, O and G alike, D34), its
 * type in *type, the body length returned, or -EAGAIN. -EMSGSIZE drops it. */
int wl_server_rx_frame(uint8_t *type, uint8_t *body, size_t max);
/* Main thread: queues the whole line plus '\n'. -ENOTCONN when no session,
 * -EINVAL when over 512 bytes, -ENOSPC after wait_ms with no room. */
int wl_server_tx_line(const char *line, int wait_ms);
bool wl_server_session_up(void);
/* Bumped on every session open, replacement and close. `live` stays true
 * across a replacement, so a stream (Task 5.1) remembers the value it began
 * under and aborts when this differs. */
uint32_t wl_server_session_gen(void);

/* Tasks 5.1 and 6.3 register the consumers of O and G frames. With none, the
 * frame closes the session: nothing is sealed or half-applied. */
typedef void (*wl_sink_fn)(const uint8_t *body, size_t n);
void wl_server_set_sink(uint8_t type, wl_sink_fn fn);
void wl_server_sink(uint8_t type, const uint8_t *body, size_t n);

/* The only line a verify-only candidate may send (S5.4), with or without its
 * newline. Pure, so tests/wl_server/host_test.c pins it. */
static inline bool wl_verify_body_ok(const uint8_t *b, size_t n)
{
	static const char want[] = "{\"t\":\"verify\"}";
	size_t w = sizeof(want) - 1;

	if (n == w + 1 && b[w] == '\n') {
		n = w;
	}
	return n == w && memcmp(b, want, w) == 0;
}

/* A candidate's opener went out whole in one non-blocking send. Pure, so
 * tests/wl_server/host_test.c pins it. */
static inline bool wl_opener_sent_whole(ssize_t k, size_t n)
{
	return k >= 0 && (size_t)k == n;
}

#endif /* WL_SERVER_H */
