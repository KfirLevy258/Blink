/*
 * Persistent config in NVS, via the settings subsystem.
 *
 * Holds the mode, the WiFi credentials, and the OAuth refresh token. The token
 * is what makes "log in once" true across power cycles -- if it does not
 * survive a reboot, the whole standalone premise collapses.
 */
#include <zephyr/kernel.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/printk.h>
#include <string.h>

#include "cfg_store.h"

#define KEY_ROOT  "claude"
#define KEY_MODE  KEY_ROOT "/mode"
#define KEY_SSID  KEY_ROOT "/ssid"
#define KEY_PSK   KEY_ROOT "/psk"
#define KEY_TOKEN KEY_ROOT "/rtok"
#define KEY_TZ    KEY_ROOT "/tzmin"

static struct {
	enum cfg_mode mode;
	char ssid[CFG_SSID_MAX];
	char psk[CFG_PSK_MAX];
	char token[CFG_TOKEN_MAX];
	int32_t tz_min;
	bool tz_set;
} cfg;

static int cfg_set_cb(const char *name, size_t len, settings_read_cb read_cb,
		      void *cb_arg)
{
	const char *next;

	if (settings_name_steq(name, "mode", &next) && !next) {
		uint8_t v = 0;

		if (read_cb(cb_arg, &v, sizeof(v)) > 0) {
			cfg.mode = (enum cfg_mode)v;
		}
		return 0;
	}
	if (settings_name_steq(name, "ssid", &next) && !next) {
		int n = read_cb(cb_arg, cfg.ssid, sizeof(cfg.ssid) - 1);

		if (n > 0) {
			cfg.ssid[n] = '\0';
		}
		return 0;
	}
	if (settings_name_steq(name, "psk", &next) && !next) {
		int n = read_cb(cb_arg, cfg.psk, sizeof(cfg.psk) - 1);

		if (n > 0) {
			cfg.psk[n] = '\0';
		}
		return 0;
	}
	if (settings_name_steq(name, "rtok", &next) && !next) {
		int n = read_cb(cb_arg, cfg.token, sizeof(cfg.token) - 1);

		if (n > 0) {
			cfg.token[n] = '\0';
		}
		return 0;
	}
	if (settings_name_steq(name, "tzmin", &next) && !next) {
		int32_t v;

		if (read_cb(cb_arg, &v, sizeof(v)) == sizeof(v)) {
			cfg.tz_min = v;
			cfg.tz_set = true;
		}
		return 0;
	}
	return -ENOENT;
}

static struct settings_handler cfg_handler = {
	.name = KEY_ROOT,
	.h_set = cfg_set_cb,
};

int cfg_init(void)
{
	int rc = settings_subsys_init();

	if (rc) {
		printk("[cfg] settings init failed: %d\n", rc);
		return rc;
	}
	rc = settings_register(&cfg_handler);
	if (rc) {
		printk("[cfg] register failed: %d\n", rc);
		return rc;
	}
	rc = settings_load();
	if (rc) {
		printk("[cfg] load failed: %d\n", rc);
		return rc;
	}

	/* Never log the token itself, only whether we have one. */
	printk("[cfg] mode=%d ssid=%s token=%s\n", cfg.mode,
	       cfg.ssid[0] ? cfg.ssid : "(none)",
	       cfg.token[0] ? "present" : "(none)");
	return 0;
}

enum cfg_mode cfg_get_mode(void)
{
	return cfg.mode;
}

int cfg_set_mode(enum cfg_mode mode)
{
	uint8_t v = (uint8_t)mode;

	cfg.mode = mode;
	return settings_save_one(KEY_MODE, &v, sizeof(v));
}

bool cfg_get_wifi(char *ssid, size_t ssid_len, char *psk, size_t psk_len)
{
	if (!cfg.ssid[0]) {
		return false;
	}
	strncpy(ssid, cfg.ssid, ssid_len - 1);
	ssid[ssid_len - 1] = '\0';
	strncpy(psk, cfg.psk, psk_len - 1);
	psk[psk_len - 1] = '\0';
	return true;
}

int cfg_set_wifi(const char *ssid, const char *psk)
{
	int rc;

	strncpy(cfg.ssid, ssid, sizeof(cfg.ssid) - 1);
	cfg.ssid[sizeof(cfg.ssid) - 1] = '\0';
	strncpy(cfg.psk, psk ? psk : "", sizeof(cfg.psk) - 1);
	cfg.psk[sizeof(cfg.psk) - 1] = '\0';

	rc = settings_save_one(KEY_SSID, cfg.ssid, strlen(cfg.ssid));
	if (rc) {
		return rc;
	}
	return settings_save_one(KEY_PSK, cfg.psk, strlen(cfg.psk));
}

bool cfg_get_token(char *tok, size_t len)
{
	if (!cfg.token[0]) {
		return false;
	}
	strncpy(tok, cfg.token, len - 1);
	tok[len - 1] = '\0';
	return true;
}

int cfg_set_token(const char *tok)
{
	/*
	 * Write-before-use. The token endpoint sometimes hands back a NEW refresh
	 * token, and the old one dies the moment the new one is used. If we used
	 * the new token before committing it and lost power in between, the chain
	 * would be broken permanently and the user would have to sign in again.
	 * So: persist first, and only report success once it is durable.
	 */
	strncpy(cfg.token, tok, sizeof(cfg.token) - 1);
	cfg.token[sizeof(cfg.token) - 1] = '\0';
	return settings_save_one(KEY_TOKEN, cfg.token, strlen(cfg.token));
}

int cfg_clear_token(void)
{
	/* Deliberately does NOT touch the WiFi credentials: a rejected token
	 * should cost the user one pasted code, not a whole re-provision.
	 */
	memset(cfg.token, 0, sizeof(cfg.token));
	return settings_delete(KEY_TOKEN);
}

int cfg_clear_wifi(void)
{
	memset(cfg.ssid, 0, sizeof(cfg.ssid));
	memset(cfg.psk, 0, sizeof(cfg.psk));
	settings_delete(KEY_SSID);
	return settings_delete(KEY_PSK);
}

bool cfg_get_tz(int32_t *offset_min)
{
	if (!cfg.tz_set) {
		return false;
	}
	*offset_min = cfg.tz_min;
	return true;
}

int cfg_set_tz(int32_t offset_min)
{
	cfg.tz_min = offset_min;
	cfg.tz_set = true;
	return settings_save_one(KEY_TZ, &cfg.tz_min, sizeof(cfg.tz_min));
}

int cfg_reset(void)
{
	memset(&cfg, 0, sizeof(cfg));
	settings_delete(KEY_MODE);
	settings_delete(KEY_SSID);
	settings_delete(KEY_PSK);
	settings_delete(KEY_TOKEN);
	settings_delete(KEY_TZ);
	return 0;
}
