/* Host test for the pure brightness stepping logic.
 *   cc -I firmware/src tests/backlight/host_test.c -o /tmp/bl && /tmp/bl
 */
#include <stdio.h>
#include "backlight.h"

static int failures;
#define CK(cond, name) do{ if(cond){printf("PASS: %s\n",name);} \
	else {printf("FAIL: %s\n",name); failures++;} }while(0)

int main(void)
{
	CK(backlight_next_level(60, +1) == 80, "up from 60 -> 80");
	CK(backlight_next_level(60, -1) == 40, "down from 60 -> 40");
	CK(backlight_next_level(80, +1) == 100, "up from 80 -> 100");
	CK(backlight_next_level(100, +1) == 100, "clamp at 100");
	CK(backlight_next_level(20, -1) == 20, "clamp at 20");
	CK(backlight_next_level(100, -1) == 80, "down from 100 -> 80");
	printf(failures ? "\n%d FAILED\n" : "\nALL PASSED\n", failures);
	return failures ? 1 : 0;
}
