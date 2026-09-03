#ifndef USAGE_FRESHNESS_H
#define USAGE_FRESHNESS_H

#include <stdint.h>

#include "usage_view.h"

/*
 * How old the figures on the screen are, how long since anything on the far
 * end spoke at all, and what the tool feeding them was doing -- the facts
 * the sleep gate needs and the ones the board used to throw away.
 *
 * proto.c parses all three out of every `usage` message (`age_s`,
 * `active_age_s`, `state`) and hands them to usage_view, which draws what it
 * draws and keeps the rest behind LVGL where main.c cannot ask and no laptop
 * can test. This holds them in a translation unit with no Zephyr and no LVGL
 * in it, for the same reason usage_state.c exists.
 *
 * The ages GROW between messages. The daemon recomputes them every 60 s, so
 * this mostly agrees with what is drawn -- but a board dozing on a stale
 * reading is deciding whether to keep dozing in the gaps, and an age frozen
 * at whatever last arrived would answer that question with the wrong number
 * for up to a minute at a time.
 */
void usage_freshness_note(int32_t age_s, int32_t active_age_s,
			  enum usage_activity act, int64_t now_ms);

/* Seconds since the shown reading was taken, or -1 when we cannot say --
 * before the first message, or from a daemon too old to send an age. Unknown
 * is not zero: a caller that treats it as a fresh reading holds the panel
 * awake against every such daemon. */
int32_t usage_freshness_age_s(int64_t now_ms);

/*
 * Seconds since ANY tool on that machine last wrote anything at all, or -1
 * when we cannot say.
 *
 * A different question from the one above, and the panel gets it wrong in a
 * way nobody can see if it answers one with the other. The daemon remembers
 * the last status line that carried a five-hour percentage and re-offers it
 * at its ORIGINAL time, because an expired window does not make the last
 * real reading untrue -- so the dial can honestly be twelve hours old five
 * seconds after Claude Code rewrote the file it came from. The first number
 * is about the READING and belongs on the caption; this one is about the
 * PERSON and is the only one the doze may key on.
 *
 * A daemon too old to send `active_age_s` gets `age_s` back from here, and
 * that substitution is exact rather than approximate: the two figures can
 * only differ because of that memory, and a daemon of that vintage does not
 * have it, so its freshest source IS the dial's source.
 */
int32_t usage_freshness_active_age_s(int64_t now_ms);

/* What the last message said the tool was doing. USAGE_ACTIVITY_NONE before
 * any message, which is the same "said nothing" the enum already means. */
enum usage_activity usage_freshness_activity(void);

#endif /* USAGE_FRESHNESS_H */
