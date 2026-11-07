/*
 * The one line a verify-only candidate may send (spec S5.4): exactly
 * {"t":"verify"}, with or without its newline, and nothing near it.
 *
 *   cc -I firmware/src -o /tmp/t tests/wl_server/host_test.c && /tmp/t
 */
#include <stdio.h>
#include <string.h>

#include "wl_server.h"

static int fails;

static void eq(const char *what, int got, int want)
{
	if (got != want) {
		fails++;
		printf("FAIL: %s (got %d, want %d)\n", what, got, want);
	} else {
		printf("PASS: %s\n", what);
	}
}

static int ok(const char *s)
{
	return wl_verify_body_ok((const uint8_t *)s, strlen(s));
}

int main(void)
{
	eq("the verify line", ok("{\"t\":\"verify\"}"), 1);
	eq("with its newline", ok("{\"t\":\"verify\"}\n"), 1);
	eq("two newlines", ok("{\"t\":\"verify\"}\n\n"), 0);
	eq("a usage line", ok("{\"t\":\"usage\"}"), 0);
	eq("a hello", ok("{\"t\":\"hello\"}"), 0);
	eq("verify with a tail", ok("{\"t\":\"verify\",\"x\":1}"), 0);
	eq("a prefix", ok("{\"t\":\"verif\"}"), 0);
	eq("empty", ok(""), 0);
	/* The opener is one non-blocking send: whole, or the candidate goes. */
	eq("opener sent whole", wl_opener_sent_whole(20, 20), 1);
	eq("opener short write", wl_opener_sent_whole(7, 20), 0);
	eq("opener EAGAIN", wl_opener_sent_whole(-1, 20), 0);
	eq("opener zero", wl_opener_sent_whole(0, 20), 0);
	return fails ? 1 : 0;
}
