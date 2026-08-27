#ifndef CFG_STORE_H
#define CFG_STORE_H

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

/* Where the board gets its numbers from. Persisted, so the choice survives a
 * power cycle -- being asked again on every boot would make it a worse device.
 */
enum cfg_mode {
	CFG_MODE_UNSET = 0,
	CFG_MODE_USB,		/* PC daemon pushes usage over serial */
	CFG_MODE_WIFI,		/* board fetches usage itself */
};

#define CFG_SSID_MAX 33		/* 32 + NUL */
#define CFG_PSK_MAX 65		/* 64 + NUL */
#define CFG_TOKEN_MAX 320	/* OAuth refresh tokens are long */

int cfg_init(void);

enum cfg_mode cfg_get_mode(void);
int cfg_set_mode(enum cfg_mode mode);

/* WiFi credentials. cfg_get_wifi() returns false if none are stored. */
bool cfg_get_wifi(char *ssid, size_t ssid_len, char *psk, size_t psk_len);
int cfg_set_wifi(const char *ssid, const char *psk);

/*
 * The OAuth refresh token.
 *
 * Stored separately from the WiFi credentials on purpose: when the token is
 * rejected and the user has to sign in again, we clear ONLY the token and keep
 * them on their network -- one pasted code, not a full re-provision.
 *
 * Secret. Never logged.
 */
bool cfg_get_token(char *tok, size_t len);
int cfg_set_token(const char *tok);
int cfg_clear_token(void);

/* Clear ONLY the WiFi credentials (settings-screen "Reset WiFi"): the token
 * survives, so after re-provisioning the network the gauges come back without
 * another sign-in. */
int cfg_clear_wifi(void);

#define CFG_AP_PSK_MAX 17	/* 16 + NUL; generated ones are 10 chars */

/* The setup AP's WPA2 password: random, generated once at first boot and
 * stable thereafter, so a printed/remembered QR keeps working. Rides in the
 * join QR, never typed. cfg_get_ap_psk() returns false until one is stored.
 * Cleared only by factory reset (a new one is generated on the next boot). */
bool cfg_get_ap_psk(char *psk, size_t len);
int cfg_set_ap_psk(const char *psk);

/* Which window the weekly gauge shows (0 = all models, 1 = fable): the
 * peek-card choice, persisted so a reboot keeps it. Defaults to 0. */
uint8_t cfg_get_weekly_sel(void);
int cfg_set_weekly_sel(uint8_t sel);

/*
 * Which provider the panel treats as primary -- the outer ring, the big
 * number, the first countdown.
 *
 * Kept on the DEVICE rather than in the daemon because it is the device the
 * user is looking at when they decide, and because a preference that resets
 * every time the daemon restarts is not a preference. The board announces it
 * to the host, which is what actually acts on it.
 */
enum cfg_main_src {
	CFG_MAIN_SRC_CLAUDE = 0,	/* also the value an unset record reads as */
	CFG_MAIN_SRC_CODEX = 1,
};

uint8_t cfg_get_main_src(void);
int cfg_set_main_src(uint8_t src);

/* Screen brightness percent, one of 20/40/60/80/100. Persisted so a reboot
 * keeps it. Returns 100 when never set (a fresh or pre-update device). */
uint8_t cfg_get_bright_pct(void);
int cfg_set_bright_pct(uint8_t pct);

/* Last known UTC offset (minutes east of UTC), fed by the tz API in WiFi mode
 * so the clock survives API outages. cfg_get_tz() returns false until an
 * offset has ever been stored. */
bool cfg_get_tz(int32_t *offset_min);
int cfg_set_tz(int32_t offset_min);

#define CFG_OTA_VER_MAX 16	/* "MAJOR.MINOR.PATCH" + NUL, with headroom */

/* OTA handshake across the update reboot: state 1 + target version are
 * written just before rebooting into a new image; the next boot compares the
 * running version to the target to report success or revert. */
uint8_t cfg_get_ota_state(char *ver, size_t len);
int cfg_set_ota_state(uint8_t st, const char *ver);

/* Wipe everything (factory reset). */
int cfg_reset(void);


/*
 * Which edition this unit is: which boot clip it plays.
 *
 * A FACTORY fact, not a preference. It is written once over USB after the
 * board is programmed (`clauge provision --edition codex`) and there is
 * deliberately no way to reach it from the settings screen -- the enclosure
 * decides it, and a user who could flip it would only be putting the wrong
 * animation in the wrong box.
 *
 * Living on the unit rather than in the binary is what keeps ONE firmware
 * image for both editions. The alternative was a second build, which forks
 * the OTA feed: the manifest names one firmware and `hello` carries no
 * edition, so a Codex unit would be offered the Claude image and would take
 * it -- its identity reverting silently, in the field, months later. Both
 * clips are compiled in instead and this picks between them at boot. The
 * second one costs 17 KB on an image using 663 KB of 4 MB.
 *
 * 0 is Claude, so an unset record -- every unit built before this existed --
 * reads as the edition that was already shipping.
 */
enum cfg_edition {
	CFG_EDITION_CLAUDE = 0,
	CFG_EDITION_CODEX = 1,
};

uint8_t cfg_get_edition(void);
int cfg_set_edition(uint8_t edition);

#endif /* CFG_STORE_H */
