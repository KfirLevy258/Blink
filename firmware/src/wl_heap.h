#ifndef WL_HEAP_H
#define WL_HEAP_H

/*
 * The one clean reboot a flaky network may cost. Every association leaks about
 * 40 B inside the WiFi blob (an esp_timer for send_wake_null_timeout created on
 * each association and never freed; power-save off and stop-before-delete both
 * failed to stop it, .superpowers/sdd/.../leak-d-report.md). With ~38 KB of
 * low-water on the system heap that is some 450 connections, so in practice a
 * router that keeps dropping for months. net_link measures after each down and
 * each automatic rejoin; main restarts the board once the number is under
 * this AND nobody would notice: no session up, no OTA open on either link (a
 * reboot mid-stream is a cut-off update), nothing on the panel a person is
 * using. Pure, so tests/wl_heap/host_test.c pins every combination.
 */
#include <stdbool.h>
#include <stddef.h>

#define WL_HEAP_LOW_BYTES 20480

static inline bool wl_heap_reboot_due(size_t free_bytes, bool session_up,
				      bool ota_open, bool modal)
{
	return free_bytes < WL_HEAP_LOW_BYTES && !session_up && !ota_open &&
	       !modal;
}

#endif /* WL_HEAP_H */
