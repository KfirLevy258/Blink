#ifndef MSG_PARSE_H
#define MSG_PARSE_H

#include <stdbool.h>
#include <stddef.h>

/* Extract the string value for `key` from a flat JSON line into buf.
 * Returns true if found. */
bool msg_get_str(const char *json, const char *key, char *buf, size_t buflen);

/* Extract a numeric value for `key`. Returns true if found (sets *out). */
bool msg_get_double(const char *json, const char *key, double *out);

/* Extract a JSON boolean (`true`/`false`) for `key`. Returns true if found
 * (sets *out). A quoted "true" is NOT a boolean and returns false, the same
 * way msg_get_double refuses a quoted number: the daemon emits real JSON
 * booleans, and accepting the string form would let a typo read as valid. */
bool msg_get_bool(const char *json, const char *key, bool *out);

#endif /* MSG_PARSE_H */
