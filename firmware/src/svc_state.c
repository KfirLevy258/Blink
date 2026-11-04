#include <string.h>

#include "svc_state.h"

/*
 * One slot per provider that has a status page, found by name. Two, fixed,
 * because the contract names two and an unknown id must not be able to take
 * a slot: "opencode" has no page, and a board that stored whatever arrived
 * would be one daemon bug away from drawing somebody else's incident.
 */
static const char *const known[] = { "claude", "codex" };
#define N_KNOWN (sizeof(known) / sizeof(known[0]))

static struct {
	enum svc_level level;
	char what[SVC_WHAT_MAX + 1];
	int64_t heard_ms;
} held[N_KNOWN];

static int slot_of(const char *provider)
{
	if (!provider) {
		return -1;
	}
	for (size_t i = 0; i < N_KNOWN; i++) {
		if (strcmp(provider, known[i]) == 0) {
			return (int)i;
		}
	}
	return -1;
}

enum svc_level svc_level_from_wire(const char *level)
{
	/*
	 * Exact words, case and all. A level this firmware has no word for is
	 * a newer daemon talking, and "ok" -- draw nothing -- is the only
	 * reading that cannot be wrong in a frightening direction. pc/protocol
	 * keeps Statuspage's own names on its side for the same reason: a
	 * vendor's sixth status should be a daemon change, not a reflash.
	 */
	if (!level) {
		return SVC_OK;
	}
	if (strcmp(level, "maint") == 0) {
		return SVC_MAINT;
	}
	if (strcmp(level, "degraded") == 0) {
		return SVC_DEGRADED;
	}
	if (strcmp(level, "partial") == 0) {
		return SVC_PARTIAL;
	}
	if (strcmp(level, "major") == 0) {
		return SVC_MAJOR;
	}
	return SVC_OK;
}

void svc_note(const char *provider, const char *level, const char *what,
	      int64_t now_ms)
{
	int i = slot_of(provider);

	if (i < 0) {
		return;
	}
	held[i].level = svc_level_from_wire(level);
	held[i].heard_ms = now_ms;
	/* Cleared on OK as well as replaced: the daemon omits `what` with the
	 * all-clear so that a stale component cannot travel with it, and
	 * keeping the old one here would undo that on this side. */
	held[i].what[0] = '\0';
	if (held[i].level != SVC_OK && what) {
		strncpy(held[i].what, what, SVC_WHAT_MAX);
		held[i].what[SVC_WHAT_MAX] = '\0';
	}
}

void svc_forget(void)
{
	memset(held, 0, sizeof(held));
}

/*
 * The panel's words, in the vendor's own vocabulary so that the owner who
 * then opens the status page finds the same phrase there. Short: the row is
 * 300 px, and the component name follows. Sentence case -- one capital.
 */
static const char *level_words(enum svc_level l)
{
	switch (l) {
	case SVC_MAINT:		return "Maintenance";
	case SVC_DEGRADED:	return "Degraded";
	case SVC_PARTIAL:	return "Partial outage";
	case SVC_MAJOR:		return "Major outage";
	default:		return "";
	}
}

enum svc_level svc_hint(const char *provider, int64_t now_ms,
			char *buf, size_t buflen)
{
	int i = slot_of(provider);
	enum svc_level l = SVC_OK;

	if (i >= 0 && held[i].level != SVC_OK &&
	    now_ms - held[i].heard_ms <= SVC_HOLD_MS) {
		l = held[i].level;
	}
	if (buf && buflen) {
		/* fmt_hint, so the component goes through fmt_ascii and the
		 * line has the same "what - which" shape as every other
		 * sentence on this row. */
		fmt_hint(level_words(l), l == SVC_OK ? "" : held[i].what,
			 buf, buflen);
	}
	return l;
}

enum fmt_pip_tone svc_tone(enum svc_level level)
{
	return level == SVC_MAJOR ? FMT_TONE_RED : FMT_TONE_AMBER;
}
