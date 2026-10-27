/*
 * The theme as the board lives with it: what is stored, what is being
 * previewed, and repainting the screens when either changes.
 *
 * theme.c decides what a theme IS (and is host-tested); this file decides
 * when one is shown and kept. Everything here runs on the main thread, which
 * is where both LVGL and proto_service() run.
 */
#ifndef UI_THEME_H
#define UI_THEME_H

#include <stdbool.h>

#include "theme.h"

/* Select the stored theme. Before any screen is built: it repaints nothing,
 * so every screen is simply born in the right colours. */
void ui_theme_init(void);

/* The id the board has STORED (not a preview): what theme_now reports. */
int ui_theme_stored_id(void);

/* The stored custom theme, if a usable one has ever been saved. */
bool ui_theme_custom_stored(struct theme_custom *out);

/* Settings picked a theme: store it, show it, tell the host. A preview from
 * the computer, if one is up, ends here. Returns false for an id it cannot
 * honour (Custom with nothing stored). */
bool ui_theme_choose(int id);

/* A validated `theme` line from the host (see theme_msg_parse). */
void ui_theme_wire(const struct theme_msg *m);

#endif /* UI_THEME_H */
