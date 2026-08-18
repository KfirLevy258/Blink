#ifndef UI_TOUCHFX_H
#define UI_TOUCHFX_H

/* A light touch "echo": a soft, half-transparent circle that appears where the
 * user pressed, expands a little and fades out. Global -- it floats on the top
 * layer over every screen and never intercepts a touch. Call once after the
 * display is up. */
void ui_touchfx_init(void);

/*
 * Stop (or resume) the echo entirely.
 *
 * Hiding the object is not enough: the poll timer keeps running and un-hides it
 * on the next press-down. The transitions and the clip player stream straight
 * to the panel's GRAM, and anything drawn on the top layer is composited into
 * that -- then repainted as flat background by whatever is underneath, which
 * punches a hole through the streamed image. Suspended for those stretches, the
 * echo simply does not happen; a press during one goes unacknowledged, which is
 * the lesser artifact.
 */
void ui_touchfx_suspend(bool on);

#endif /* UI_TOUCHFX_H */
