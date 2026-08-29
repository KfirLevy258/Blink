#ifndef UI_SLEEP_H
#define UI_SLEEP_H

/* The computer has gone to sleep: close the eyes, doze until the app speaks
 * again, open them. Blocks for the whole of it, servicing the daemon protocol
 * between frames, and returns with the previous screen restored. A tap while
 * dozing shows the dashboard with a note for ten seconds. */
void ui_sleep_run(void);

#endif /* UI_SLEEP_H */
