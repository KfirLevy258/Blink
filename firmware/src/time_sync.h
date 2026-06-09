#ifndef TIME_SYNC_H
#define TIME_SYNC_H

/* Query an SNTP server and set the system real-time clock.
 * Returns 0 on success, negative errno otherwise. */
int time_sync_now(const char *server, int timeout_ms);

#endif /* TIME_SYNC_H */
