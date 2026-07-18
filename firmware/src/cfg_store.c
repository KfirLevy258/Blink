/*
 * Persistent config in a self-owned A/B flash record -- deliberately NOT
 * settings/NVS.
 *
 * With flash encryption enabled, erased-but-unwritten flash reads back as
 * decryption garbage, never 0xFF. NVS's on-flash format leans entirely on
 * "erased reads as the erase value", so it cannot even mount (-EDEADLK on
 * hardware, 2026-07-17); ESP-IDF exempts NVS from flash encryption for the
 * same reason. This store leans the other way: one sealed record (magic +
 * sequence + CRC32) whose unreadable bytes simply fail the seal and mean
 * "empty" -- garbage-as-erased is the design, not a failure mode.
 *
 * Two alternating 4 KB sectors make writes power-loss safe: a new record
 * goes to the OTHER sector, and the old one stays valid until the new seal
 * is fully on flash. Mount picks the valid record with the higher sequence.
 *
 * Every byte rides the chip's transparent encrypted write path, so the
 * refresh token is ciphertext at rest -- the point of the whole exercise.
 * Write volume is trivial (token rotation every few hours, tz daily), so
 * two fixed sectors need no wear leveling.
 */
#include <zephyr/kernel.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/printk.h>
#include <string.h>

#include "cfg_store.h"

#define REC_MAGIC 0xC1A46EF6u
#define SECTOR 4096
#define SLOT_A 0
#define SLOT_B SECTOR

/*
 * The standalone net worker rotates tokens and stores tz results while the
 * main (LVGL) thread can clear config from the settings panel. Every entry
 * point takes this lock; persist() runs under it too (an erase+write is
 * tens of milliseconds -- one dropped frame at worst, on rare writes).
 */
static K_MUTEX_DEFINE(cfg_lock);

static const struct flash_area *fa;

struct rec {
	uint32_t magic;
	uint32_t seq;
	uint8_t mode;
	uint8_t weekly_sel;
	uint8_t tz_set;
	uint8_t bright_pct;	/* 0 = unset -> 100% default; else 20/40/60/80/100 */
	int32_t tz_min;
	char ssid[CFG_SSID_MAX];
	char psk[CFG_PSK_MAX];
	char token[CFG_TOKEN_MAX];
	char ap_psk[CFG_AP_PSK_MAX];
	uint32_t crc;		/* over everything above, always last */
} __packed;

/* On-flash footprint, padded to the 32-byte encrypted-write block. */
#define REC_WIRE ROUND_UP(sizeof(struct rec), 32)

static struct rec cfg;		/* RAM mirror; seq/crc only meaningful on flash */
static int cur_slot = -1;	/* live record's offset; -1 = none yet */

static uint32_t rec_crc(const struct rec *r)
{
	return crc32_ieee((const uint8_t *)r, offsetof(struct rec, crc));
}

static bool slot_load(int off, struct rec *out)
{
	uint8_t buf[REC_WIRE];

	if (flash_area_read(fa, off, buf, sizeof(buf)) != 0) {
		return false;
	}
	memcpy(out, buf, sizeof(*out));
	return out->magic == REC_MAGIC && out->crc == rec_crc(out);
}

/* Called with cfg_lock held. */
static int persist(void)
{
	static uint8_t buf[REC_WIRE];
	int next = (cur_slot == SLOT_A) ? SLOT_B : SLOT_A;

	cfg.magic = REC_MAGIC;
	cfg.seq++;
	cfg.crc = rec_crc(&cfg);

	memset(buf, 0, sizeof(buf));
	memcpy(buf, &cfg, sizeof(cfg));

	int rc = flash_area_erase(fa, next, SECTOR);

	if (rc == 0) {
		rc = flash_area_write(fa, next, buf, sizeof(buf));
	}
	if (rc == 0) {
		cur_slot = next;
	} else {
		printk("[cfg] persist failed: %d\n", rc);
	}
	return rc;
}

int cfg_init(void)
{
	int rc = flash_area_open(FIXED_PARTITION_ID(storage_partition), &fa);

	if (rc) {
		printk("[cfg] storage open failed: %d\n", rc);
		return rc;
	}

	struct rec a, b;
	bool va = slot_load(SLOT_A, &a);
	bool vb = slot_load(SLOT_B, &b);

	if (va && (!vb || a.seq >= b.seq)) {
		cfg = a;
		cur_slot = SLOT_A;
	} else if (vb) {
		cfg = b;
		cur_slot = SLOT_B;
	} else {
		memset(&cfg, 0, sizeof(cfg));
		cur_slot = -1;	/* first persist() lands in slot A */
	}

	/* Never log the token itself, only whether we have one. */
	printk("[cfg] mode=%d ssid=%s token=%s\n", cfg.mode,
	       cfg.ssid[0] ? cfg.ssid : "(none)",
	       cfg.token[0] ? "present" : "(none)");
	return 0;
}

enum cfg_mode cfg_get_mode(void)
{
	return (enum cfg_mode)cfg.mode;
}

int cfg_set_mode(enum cfg_mode mode)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	cfg.mode = (uint8_t)mode;

	int rc = persist();

	k_mutex_unlock(&cfg_lock);
	return rc;
}

bool cfg_get_wifi(char *ssid, size_t ssid_len, char *psk, size_t psk_len)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	if (!cfg.ssid[0]) {
		k_mutex_unlock(&cfg_lock);
		return false;
	}
	strncpy(ssid, cfg.ssid, ssid_len - 1);
	ssid[ssid_len - 1] = '\0';
	strncpy(psk, cfg.psk, psk_len - 1);
	psk[psk_len - 1] = '\0';
	k_mutex_unlock(&cfg_lock);
	return true;
}

int cfg_set_wifi(const char *ssid, const char *psk)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	strncpy(cfg.ssid, ssid, sizeof(cfg.ssid) - 1);
	cfg.ssid[sizeof(cfg.ssid) - 1] = '\0';
	strncpy(cfg.psk, psk ? psk : "", sizeof(cfg.psk) - 1);
	cfg.psk[sizeof(cfg.psk) - 1] = '\0';

	int rc = persist();

	k_mutex_unlock(&cfg_lock);
	return rc;
}

bool cfg_get_token(char *tok, size_t len)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	if (!cfg.token[0]) {
		k_mutex_unlock(&cfg_lock);
		return false;
	}
	strncpy(tok, cfg.token, len - 1);
	tok[len - 1] = '\0';
	k_mutex_unlock(&cfg_lock);
	return true;
}

int cfg_set_token(const char *tok)
{
	/*
	 * Write-before-use. The token endpoint sometimes hands back a NEW
	 * refresh token, and the old one dies the moment the new one is
	 * used. Persist first; only report success once it is durable.
	 * (The A/B scheme keeps the OLD token valid on flash until the new
	 * record seals -- a power cut mid-persist costs nothing.)
	 */
	k_mutex_lock(&cfg_lock, K_FOREVER);
	strncpy(cfg.token, tok, sizeof(cfg.token) - 1);
	cfg.token[sizeof(cfg.token) - 1] = '\0';

	int rc = persist();

	k_mutex_unlock(&cfg_lock);
	return rc;
}

int cfg_clear_token(void)
{
	/* Deliberately does NOT touch the WiFi credentials: a rejected token
	 * should cost the user one pasted code, not a whole re-provision.
	 */
	k_mutex_lock(&cfg_lock, K_FOREVER);
	memset(cfg.token, 0, sizeof(cfg.token));

	int rc = persist();

	k_mutex_unlock(&cfg_lock);
	return rc;
}

int cfg_clear_wifi(void)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	memset(cfg.ssid, 0, sizeof(cfg.ssid));
	memset(cfg.psk, 0, sizeof(cfg.psk));

	int rc = persist();

	k_mutex_unlock(&cfg_lock);
	return rc;
}

uint8_t cfg_get_weekly_sel(void)
{
	return cfg.weekly_sel;
}

int cfg_set_weekly_sel(uint8_t sel)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	cfg.weekly_sel = sel;

	int rc = persist();

	k_mutex_unlock(&cfg_lock);
	return rc;
}

uint8_t cfg_get_bright_pct(void)
{
	return cfg.bright_pct == 0 ? 100 : cfg.bright_pct;
}

int cfg_set_bright_pct(uint8_t pct)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	cfg.bright_pct = pct;

	int rc = persist();

	k_mutex_unlock(&cfg_lock);
	return rc;
}

bool cfg_get_ap_psk(char *psk, size_t len)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	if (!cfg.ap_psk[0]) {
		k_mutex_unlock(&cfg_lock);
		return false;
	}
	strncpy(psk, cfg.ap_psk, len - 1);
	psk[len - 1] = '\0';
	k_mutex_unlock(&cfg_lock);
	return true;
}

int cfg_set_ap_psk(const char *psk)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	strncpy(cfg.ap_psk, psk, sizeof(cfg.ap_psk) - 1);
	cfg.ap_psk[sizeof(cfg.ap_psk) - 1] = '\0';

	int rc = persist();

	k_mutex_unlock(&cfg_lock);
	return rc;
}

bool cfg_get_tz(int32_t *offset_min)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	if (!cfg.tz_set) {
		k_mutex_unlock(&cfg_lock);
		return false;
	}
	*offset_min = cfg.tz_min;
	k_mutex_unlock(&cfg_lock);
	return true;
}

int cfg_set_tz(int32_t offset_min)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	cfg.tz_min = offset_min;
	cfg.tz_set = 1;

	int rc = persist();

	k_mutex_unlock(&cfg_lock);
	return rc;
}

int cfg_reset(void)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	memset(&cfg, 0, sizeof(cfg));
	flash_area_erase(fa, SLOT_A, SECTOR);
	flash_area_erase(fa, SLOT_B, SECTOR);
	cur_slot = -1;
	k_mutex_unlock(&cfg_lock);
	return 0;
}
