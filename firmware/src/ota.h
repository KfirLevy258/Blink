#ifndef OTA_H
#define OTA_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include "ota_parse.h"

/* All blocking calls run on the net-worker thread ONLY (it owns every
 * blocking network call in standalone mode). The UI talks to this module
 * exclusively through the request flags + snapshot below. */

enum ota_result {
	OTA_OK = 0,
	OTA_ERR_NET = -1,	/* DNS/TCP/TLS failure */
	OTA_ERR_HTTP = -2,	/* unexpected status */
	OTA_ERR_PARSE = -3,	/* manifest/redirect malformed */
	OTA_ERR_FLASH = -4,	/* slot1 write failed */
	OTA_ERR_HASH = -5,	/* SHA-256 mismatch */
	OTA_ERR_SIZE = -6,	/* image exceeds slot or truncated */
};

/* Blocking: resolve latest release. OTA_OK + newer==true means out holds a
 * candidate strictly newer than the running version (soft anti-rollback:
 * older-or-equal reports newer==false, never installs). */
enum ota_result ota_check(struct ota_manifest *out, bool *newer);

/* Blocking: stream blink-fw.bin into slot1, verify sha256, mark pending.
 * On OTA_OK the caller persists cfg_set_ota_state(1, m->version) and
 * reboots. Progress lands in the snapshot for the UI. */
enum ota_result ota_install(const struct ota_manifest *m);

/* The manifest from the most recent successful ota_check(), so an install
 * uses exactly the checked sha256 (the UI snapshot doesn't carry it). */
void ota_last_manifest(struct ota_manifest *out);

/* --- UI <-> worker handshake (all thread-safe) --- */
enum ota_ui_state {
	OTA_UI_IDLE, OTA_UI_CHECKING, OTA_UI_UP_TO_DATE, OTA_UI_AVAILABLE,
	OTA_UI_DOWNLOADING, OTA_UI_REBOOTING, OTA_UI_FAILED,
};

struct ota_ui {
	enum ota_ui_state st;
	char version[16];	/* candidate version when AVAILABLE+ */
	uint32_t size;
	uint8_t pct;		/* download progress 0..100 */
	/* Why it failed, when anything knows. The daemon already sends this in
	 * ota_error and it was being thrown away, so every cause -- a hash
	 * that did not match, a download that never started, a chip we refuse
	 * to write -- arrived on screen as the same four words. */
	char err[48];
};

void ota_ui_get(struct ota_ui *out);
/* Which link an install is running over. The progress screen needs it because
 * the two behave differently: over WiFi the board downloads and can show a
 * percentage, while over USB the daemon runs esptool and the board is not part
 * of the transfer at all, so it has no byte count to report. */
enum ota_source {
	OTA_SRC_WIFI,
	OTA_SRC_USB,
};

enum ota_source ota_ui_source(void);
void ota_ui_set_source(enum ota_source src);

void ota_request_check(void);	/* UI: settings tile tapped */
void ota_request_install(void);	/* UI: user confirmed install */
bool ota_take_check_request(void);	/* worker side */
bool ota_take_install_request(void);
void ota_ui_set(enum ota_ui_state st, const struct ota_manifest *m, uint8_t pct);
/* Attach a reason to the next OTA_UI_FAILED. Cleared by any other state. */
void ota_ui_set_error(const char *why);
bool ota_badge(void);	/* daily check found something (survives panel close) */

#endif /* OTA_H */
