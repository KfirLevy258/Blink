#ifndef USAGE_VIEW_H
#define USAGE_VIEW_H

/* Update the latest usage snapshot and print it to the console. */
void usage_view_update(double session_pct, const char *session_resets_at,
		       double weekly_pct, const char *weekly_resets_at);

#endif /* USAGE_VIEW_H */
