#ifndef USAGE_FRESHNESS_H
#define USAGE_FRESHNESS_H

#include <stdint.h>

#include "usage_view.h"

/*
 * How old the figures on the screen are, and what the tool feeding them was
 * doing -- the two facts the sleep gate needs and the only two the board
 * used to throw away.
 *
 * proto.c parses both out of every `usage` message (`age_s`, `state`) and
 * hands them to usage_view, which draws them and keeps them behind LVGL
 * where main.c cannot ask and no laptop can test. This holds the same two
 * values in a translation unit with no Zephyr and no LVGL in it, for the
 * same reason usage_state.c exists.
 *
 * The age GROWS between messages. The daemon recomputes it every 60 s, so
 * this mostly agrees with what is drawn -- but a board dozing on a stale
 * reading is deciding whether to keep dozing in the gaps, and an age frozen
 * at whatever last arrived would answer that question with the wrong number
 * for up to a minute at a time.
 */
void usage_freshness_note(int32_t age_s, enum usage_activity act,
			  int64_t now_ms);

/* Seconds since the shown reading was taken, or -1 when we cannot say --
 * before the first message, or from a daemon too old to send an age. Unknown
 * is not zero: a caller that treats it as a fresh reading holds the panel
 * awake against every such daemon. */
int32_t usage_freshness_age_s(int64_t now_ms);

/* What the last message said the tool was doing. USAGE_ACTIVITY_NONE before
 * any message, which is the same "said nothing" the enum already means. */
enum usage_activity usage_freshness_activity(void);

#endif /* USAGE_FRESHNESS_H */
