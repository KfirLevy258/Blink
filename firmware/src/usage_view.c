#include <zephyr/kernel.h>
#include "usage_view.h"

void usage_view_update(double session_pct, const char *session_resets_at,
		       double weekly_pct, const char *weekly_resets_at)
{
	printk("[usage] Session (5h): %5.1f%%   resets %s\n",
	       session_pct, session_resets_at);
	printk("[usage] Weekly  (7d): %5.1f%%   resets %s\n",
	       weekly_pct, weekly_resets_at);
}
