#ifndef UI_SLEEP_H
#define UI_SLEEP_H

#include <stdbool.h>

/*
 * Close the eyes, doze until `awake()` says otherwise, open them. Blocks for
 * the whole of it, servicing the daemon protocol between frames, and returns
 * with the previous screen restored. A tap while dozing shows the dashboard
 * with `peek_note` under it for ten seconds.
 *
 * `awake` is the caller's, not this file's, because there are two reasons to
 * doze and they end differently. A computer that went silent wakes when it
 * speaks. A computer whose daemon never stopped talking but has had nothing
 * new to say for hours cannot use that test at all -- it is true the whole
 * time it is dozing -- so it asks about the age of the reading instead. This
 * function used to hard-code the first test, which is why the second kind of
 * sleep could not be built on it.
 *
 * Must not be NULL: a doze with no way out is a bricked panel.
 */
void ui_sleep_run(bool (*awake)(void), const char *peek_note);

#endif /* UI_SLEEP_H */
