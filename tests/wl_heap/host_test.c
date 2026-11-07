/*
 * When a board with a leaking WiFi blob may restart itself (wl_heap.h).
 *
 *   cc -I firmware/src -o /tmp/t tests/wl_heap/host_test.c && /tmp/t
 */
#include <stdio.h>

#include "wl_heap.h"

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

int main(void)
{
	eq("low and idle: restart", wl_heap_reboot_due(1000, false, false, false), 1);
	eq("exactly at the line: no", wl_heap_reboot_due(20480, false, false, false), 0);
	eq("one byte under: yes", wl_heap_reboot_due(20479, false, false, false), 1);
	eq("plenty: no", wl_heap_reboot_due(60000, false, false, false), 0);
	eq("a session is up: wait", wl_heap_reboot_due(1000, true, false, false), 0);
	eq("an OTA is open: wait", wl_heap_reboot_due(1000, false, true, false), 0);
	eq("the panel is in use: wait", wl_heap_reboot_due(1000, false, false, true), 0);
	eq("everything busy: wait", wl_heap_reboot_due(0, true, true, true), 0);
	return fails ? 1 : 0;
}
