#ifndef VERSION_H
#define VERSION_H

/* One source of truth: the serial hello and the settings panel must never
 * disagree about what is flashed. Bump on any user-visible change. */
#define CLAUGE_FW_VERSION "0.4.2"

/* No accepted wall-clock time may precede this firmware's existence
 * (2026-07-17T00:00:00Z). SNTP is unauthenticated UDP; without a floor, an
 * on-path attacker can shift the clock into the past and make once-valid
 * leaked certificates verify again. Nudge forward on version bumps.
 * 1784937600 = 2026-07-25T00:00:00Z. */
#define CLAUGE_TIME_FLOOR 1784937600LL

#endif /* VERSION_H */
