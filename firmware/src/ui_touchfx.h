#ifndef UI_TOUCHFX_H
#define UI_TOUCHFX_H

/* A light touch "echo": a soft, half-transparent circle that appears where the
 * user pressed, expands a little and fades out. Global -- it floats on the top
 * layer over every screen and never intercepts a touch. Call once after the
 * display is up. */
void ui_touchfx_init(void);

#endif /* UI_TOUCHFX_H */
