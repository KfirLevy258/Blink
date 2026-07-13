#ifndef CFG_STORE_H
#define CFG_STORE_H

#include <stddef.h>
#include <stdbool.h>

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

/* Wipe everything (factory reset). */
int cfg_reset(void);

#endif /* CFG_STORE_H */
