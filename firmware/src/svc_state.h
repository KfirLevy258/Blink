#ifndef SVC_STATE_H
#define SVC_STATE_H

#include <stddef.h>
#include <stdint.h>

#include "fmt.h"

/*
 * What the provider's public status page says, per provider.
 *
 * The daemon reads status.claude.com and status.openai.com and sends one
 * `svc` message per page the board is showing (pc/protocol.py svc(), whose
 * docstring is the contract this file is built to). This holds the last word
 * for each provider and turns it into the sentence the hint line draws.
 *
 * Its own translation unit, with no Zephyr and no LVGL, so
 * tests/svc_state/host_test.c can reach every decision in it on a laptop --
 * the same reason usage_state.c exists. usage_view.c only asks and colours.
 */

/* The wire's five levels, mildest first. Anything else reads as OK. */
enum svc_level {
	SVC_OK = 0,
	SVC_MAINT,
	SVC_DEGRADED,
	SVC_PARTIAL,
	SVC_MAJOR,
};

/*
 * How long a non-OK level stands without being said again: SVC_HOLD_S in
 * pc/protocol.py, two and a half of the daemon's 60 s heartbeats. An incident
 * must never outlive the daemon that reported it -- an uninstalled app, or a
 * provider its bus has quarantined, would otherwise leave "Major outage" on
 * somebody's desk for good.
 */
#define SVC_HOLD_MS 150000

/* pc/protocol.py SVC_WHAT_MAX_BYTES. The longest name the daemon sends
 * today is "Claude Code", 11. */
#define SVC_WHAT_MAX 24

enum svc_level svc_level_from_wire(const char *level);

/*
 * Record one `svc` message, heard at `now_ms` (uptime). A provider other than
 * "claude" or "codex" is ignored: the contract names only those two, and a
 * board should drop one it has no page for rather than store it somewhere a
 * page could find it. `what` may be NULL or empty.
 */
void svc_note(const char *provider, const char *level, const char *what,
	      int64_t now_ms);

/* Forget everything, as at boot. */
void svc_forget(void);

/*
 * The level for `provider` at `now_ms`, after the hold, and the hint-line
 * sentence for it in `buf` ("" at OK): "Major outage - Claude Code". `buf`
 * may be NULL to ask for the level alone.
 *
 * Keyed by provider, never by page slot: the pages swap when the owner picks
 * another main provider, and the second one comes and goes, so a slot would
 * put Claude's incident on the Codex page.
 */
enum svc_level svc_hint(const char *provider, int64_t now_ms,
			char *buf, size_t buflen);

/* Amber for maintenance, degraded and partial; red for a major outage. */
enum fmt_pip_tone svc_tone(enum svc_level level);

#endif /* SVC_STATE_H */
