#ifndef USAGE_PARSE_H
#define USAGE_PARSE_H

#include <stdbool.h>
#include <stddef.h>

struct usage_window {
	bool present;
	double utilization;   /* 0..100 */
	char resets_at[40];   /* ISO8601 string, empty if absent */
};

struct usage_data {
	struct usage_window five_hour;
	struct usage_window seven_day;
	struct usage_window seven_day_fable;	/* current accounts */
	struct usage_window seven_day_sonnet;	/* older window split */
	struct usage_window seven_day_opus;
};

/* Parse the usage JSON body. Returns 0 on success (five_hour + seven_day found),
 * negative on malformed input or missing mandatory fields. */
int usage_parse(const char *json, size_t len, struct usage_data *out);

#endif /* USAGE_PARSE_H */
