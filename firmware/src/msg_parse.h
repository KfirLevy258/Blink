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

/* The raw text just after `"key":`, for values the getters above do not
 * cover -- arrays, mostly. NULL if `key` is not present as a key. The caller
 * parses from there and must stop at the end of its own value. */
const char *msg_find_value(const char *json, const char *key);

#endif /* MSG_PARSE_H */
