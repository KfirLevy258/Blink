/*
 * What the settings panel and the main loop share about an update: the
 * snapshot the row and the progress screen read, and the request flags the
 * row's taps set. The image itself arrives through ota_net.c (spec S9).
 *
 * The HTTPS half that used to live here -- https_get, fetch_follow, the
 * GitHub certificates -- is gone (spec S9, Task 5.1): with a network stack in
 * the image it was one linker decision away from a board that fetches from
 * GitHub by itself, which is the standalone mode by another name.
 */
#include <zephyr/kernel.h>
#include <string.h>

#include "ota.h"

/* --- UI snapshot + request flags --- */
static struct ota_ui ui;
static struct k_spinlock ui_lock;
static atomic_t req_check;
static atomic_t req_install;
static atomic_t badge;

static enum ota_source ui_src = OTA_SRC_WIFI;

enum ota_source ota_ui_source(void)
{
	return ui_src;
}

void ota_ui_set_source(enum ota_source src)
{
	ui_src = src;
}

/* --- UI <-> worker handshake --- */

void ota_ui_get(struct ota_ui *out)
{
	k_spinlock_key_t key = k_spin_lock(&ui_lock);

	*out = ui;
	k_spin_unlock(&ui_lock, key);
}

void ota_ui_set(enum ota_ui_state st, const struct ota_manifest *m,
		uint8_t pct)
{
	k_spinlock_key_t key = k_spin_lock(&ui_lock);

	ui.st = st;
	if (m) {
		strncpy(ui.version, m->version, sizeof(ui.version) - 1);
		ui.version[sizeof(ui.version) - 1] = '\0';
		ui.size = m->size;
	}
	ui.pct = pct;
	if (st != OTA_UI_FAILED) {
		ui.err[0] = '\0';	/* a reason outlives only its failure */
	}
	k_spin_unlock(&ui_lock, key);

	if (st == OTA_UI_AVAILABLE) {
		atomic_set(&badge, 1);
	} else if (st == OTA_UI_UP_TO_DATE || st == OTA_UI_REBOOTING) {
		atomic_set(&badge, 0);
	}
}

void ota_ui_set_error(const char *why)
{
	k_spinlock_key_t key = k_spin_lock(&ui_lock);

	if (why && why[0]) {
		strncpy(ui.err, why, sizeof(ui.err) - 1);
		ui.err[sizeof(ui.err) - 1] = '\0';
	} else {
		ui.err[0] = '\0';
	}
	k_spin_unlock(&ui_lock, key);
}

void ota_request_check(void)
{
	atomic_set(&req_check, 1);
}

void ota_request_install(void)
{
	/*
	 * The badge is NOT cleared here any more.
	 *
	 * Clearing it at consent meant the board forgot there was an update
	 * the instant the user asked for one -- before anything had been
	 * sent, let alone accepted. On every path where the install then did
	 * not happen (a daemon that had gone away, an ota_staged cleared out
	 * from under the tap -- see upd_tap.h) the row went blank and the one
	 * affordance for trying again went with it.
	 *
	 * ota_ui_set() already clears it at REBOOTING, which is the point of
	 * no return, and at UP_TO_DATE, which is a check saying there is
	 * nothing to badge. Those are the two places it is actually true.
	 */
	atomic_set(&req_install, 1);
}

bool ota_take_check_request(void)
{
	return atomic_set(&req_check, 0) != 0;
}

bool ota_take_install_request(void)
{
	return atomic_set(&req_install, 0) != 0;
}

bool ota_badge(void)
{
	return atomic_get(&badge) != 0;
}
