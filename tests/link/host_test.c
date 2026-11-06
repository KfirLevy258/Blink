/* Host test for link arbitration.
 *   cc -I firmware/src tests/link/host_test.c firmware/src/link.c -o /tmp/lk
 */
#include <stdio.h>
#include "link.h"

static int failures;
#define CK(cond, name) do{ if(cond){printf("PASS: %s\n",name);} \
	else {printf("FAIL: %s\n",name); failures++;} }while(0)

/* The module is all statics; each test starts from a known state. */
static void reset(void)
{
	link_wifi_up(false);
	link_auth_ok();
}
/* No reset for usb_ever/usb_ms: tests use a fresh epoch instead. */
static int64_t epoch;
static int64_t T(int64_t ms) { return epoch + ms; }
static void next_test(void)
{
	reset();
	epoch += 1000000; /* far past any timeout, backoff or window */
	link_usb_heard(epoch - 900000); /* stale: silent by now */
}

int main(void)
{
	/* The first test sees the true power-on state. */
	CK(link_active(0) == LINK_NONE, "usb_default_when_nothing: none");
	CK(link_wifi_may_accept(0) == LINK_ACCEPT_SESSION, "nothing heard: accept");
	epoch = 0;

	next_test();
	link_wifi_up(true);
	CK(link_active(T(0)) == LINK_WIFI, "wifi_when_usb_silent");

	next_test();
	link_wifi_up(true);
	link_usb_heard(T(0));
	CK(link_wifi_must_close(T(1)), "usb_speaking_closes_wifi: must close");
	CK(link_active(T(1)) == LINK_USB, "usb_speaking_closes_wifi: usb wins");

	next_test();
	link_usb_heard(T(0));
	CK(link_wifi_may_accept(T(29999)) == LINK_ACCEPT_NO, "accept: no at 29999");
	CK(link_wifi_may_accept(T(30001)) == LINK_ACCEPT_SESSION,
	   "wifi_accept_resumes_after_usb_timeout: yes at 30001");

	next_test();
	{
		static const int want[] = {1, 2, 4, 8, 16, 32, 60, 60, 60};
		int ok = 1;
		for (unsigned i = 0; i < sizeof(want) / sizeof(want[0]); i++) {
			int64_t ms = want[i] * 1000;
			link_auth_failed(T(0));
			if (link_wifi_may_accept(T(ms - 1)) != LINK_ACCEPT_NO ||
			    link_wifi_may_accept(T(ms)) != LINK_ACCEPT_SESSION) {
				ok = 0;
			}
		}
		CK(ok, "backoff_doubles_to_60");
	}
	link_auth_ok();
	link_auth_failed(T(0));
	CK(link_wifi_may_accept(T(1000)) == LINK_ACCEPT_SESSION,
	   "backoff_reset_on_success: back to 1 s");
	CK(link_wifi_may_accept(T(0)) == LINK_ACCEPT_NO, "backoff_reset: still running");

	CK(!link_replace_session(false) && link_replace_session(true),
	   "replace_only_on_auth");

	next_test();
	link_wifi_up(true);
	link_auth_failed(T(0));
	CK(link_active(T(1)) == LINK_WIFI && !link_wifi_must_close(T(1)),
	   "live_session_ignores_backoff");

	next_test();
	link_usb_heard(T(0));
	link_verify_window(T(0));
	CK(link_wifi_may_accept(T(1000)) == LINK_ACCEPT_VERIFY_ONLY,
	   "verify_only: usb active, window open");
	link_usb_heard(T(59000));
	CK(link_wifi_may_accept(T(60001)) == LINK_ACCEPT_NO, "verify_only: 60001 ms later no");
	next_test();
	link_usb_heard(T(0));
	link_verify_window(T(0));
	link_auth_ok();
	CK(link_wifi_may_accept(T(1000)) == LINK_ACCEPT_NO, "verify_only: after one session no");
	next_test();
	link_verify_window(T(0));
	CK(link_wifi_may_accept(T(1000)) == LINK_ACCEPT_SESSION,
	   "verify_only: usb silent with window open -> session");

	next_test();
	link_usb_heard(T(0));
	link_verify_window(T(0));
	CK(link_active(T(1000)) == LINK_USB, "verify_session_never_becomes_active: before");
	CK(link_wifi_may_accept(T(1000)) == LINK_ACCEPT_VERIFY_ONLY &&
	   link_active(T(1001)) == LINK_USB, "verify_session_never_becomes_active: during");
	link_auth_ok();
	CK(link_active(T(1002)) == LINK_USB, "verify_session_never_becomes_active: after");

	printf(failures ? "\n%d FAILED\n" : "\nALL PASSED\n", failures);
	return failures ? 1 : 0;
}
