/* The dozing rule, pinned: docs/sleep-mode-design.md. */
#include <stdio.h>
#include "sleep_gate.h"

static int fails;
#define CHECK(c) do { if (!(c)) { fails++; printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

int main(void)
{
	/* the case it exists for: app silent, figures shown, nothing flashing */
	CHECK(sleep_should_start(true, true, false));
	/* never met the app this boot: still "connecting" */
	CHECK(!sleep_should_start(true, false, false));
	/* the app is talking, or said bye: no sleep */
	CHECK(!sleep_should_start(false, true, false));
	/* esptool has the port: silence means an update, not a nap */
	CHECK(!sleep_should_start(true, true, true));
	printf("%s\n", fails ? "FAIL" : "ok   sleep_gate");
	return fails ? 1 : 0;
}
