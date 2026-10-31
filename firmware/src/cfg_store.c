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
#include <errno.h>
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
	uint8_t ota_state;		/* 0 idle, 1 install started (cleared on next boot) */
	char ota_target[CFG_OTA_VER_MAX]; /* version the install aimed at */
	uint8_t main_src;		/* 0 unset -> claude; else enum cfg_main_src */
	uint8_t edition;		/* 0 unset -> claude; else enum cfg_edition */
	uint8_t edition_locked;		/* 1 once stamped; see cfg_set_edition */
	/*
	 * 1.3.4's seal, kept where 1.3.4 looks for it.
	 *
	 * Everything above this line is the 1.3.4 record, byte for byte, and
	 * this is its CRC over exactly those bytes. Every earlier field
	 * addition moved the CRC to the new end, which kept UPDATES working
	 * (each layout got a reader below) but meant a DOWNGRADE found a
	 * record whose CRC was in the wrong place and treated it as blank --
	 * losing brightness, timezone and, worse, the edition latch, which a
	 * blank record does not have. The theme fields are the first to be
	 * appended with a downgrade promised (orch contract 2026-10-27), so
	 * they are appended AFTER a still-valid 1.3.4 record instead of
	 * inside one. tests/cfg_store plays both directions.
	 */
	uint32_t crc_134;
	uint8_t theme_id;		/* 0 Dark; see struct cfg_theme */
	uint8_t theme_rgb[21];
	uint8_t theme_w;
	uint8_t theme_ends;
	uint8_t theme_dot;
	uint8_t theme_by;
	uint8_t focus_on;		/* Focus mode; see cfg_get_focus_on */
	uint8_t focus_win;
	uint32_t crc;		/* over everything above, always last */
} __packed;

/* The layout before the edition latch. Everything else is identical, so the
 * only question a migration has to answer is whether the record it found had
 * already been stamped -- see slot_load. */
struct rec_pre_lock {
	uint32_t magic;
	uint32_t seq;
	uint8_t mode;
	uint8_t weekly_sel;
	uint8_t tz_set;
	uint8_t bright_pct;
	int32_t tz_min;
	char ssid[CFG_SSID_MAX];
	char psk[CFG_PSK_MAX];
	char token[CFG_TOKEN_MAX];
	char ap_psk[CFG_AP_PSK_MAX];
	uint8_t ota_state;
	char ota_target[CFG_OTA_VER_MAX];
	uint8_t main_src;
	uint8_t edition;
	uint32_t crc;
} __packed;

/* The layout before `edition` was added -- see the note on rec_pre_src for
 * why every field addition needs one of these. */
struct rec_pre_edition {
	uint32_t magic;
	uint32_t seq;
	uint8_t mode;
	uint8_t weekly_sel;
	uint8_t tz_set;
	uint8_t bright_pct;
	int32_t tz_min;
	char ssid[CFG_SSID_MAX];
	char psk[CFG_PSK_MAX];
	char token[CFG_TOKEN_MAX];
	char ap_psk[CFG_AP_PSK_MAX];
	uint8_t ota_state;
	char ota_target[CFG_OTA_VER_MAX];
	uint8_t main_src;
	uint32_t crc;
} __packed;

/*
 * The layout before main_src was added.
 *
 * Every field addition needs one of these, because the CRC spans everything
 * ahead of it: a record sealed by the previous firmware has both a different
 * length and a different CRC offset, so it fails validation outright. Without
 * a reader for the old shape an update looks exactly like a corrupt record --
 * and this record holds the WiFi credentials and the token. Losing it silently
 * is the worst thing this file can do.
 */
struct rec_pre_src {
	uint32_t magic;
	uint32_t seq;
	uint8_t mode;
	uint8_t weekly_sel;
	uint8_t tz_set;
	uint8_t bright_pct;
	int32_t tz_min;
	char ssid[CFG_SSID_MAX];
	char psk[CFG_PSK_MAX];
	char token[CFG_TOKEN_MAX];
	char ap_psk[CFG_AP_PSK_MAX];
	uint8_t ota_state;
	char ota_target[CFG_OTA_VER_MAX];
	uint32_t crc;
} __packed;

/* Pre-OTA layout (shipped 0.3.0). A record sealed by that firmware fails the
 * new CRC span; this loader keeps it readable so an update never wipes the
 * user's WiFi/token. Remove only when no 0.3.x board can still exist. */
struct rec_legacy {
	uint32_t magic;
	uint32_t seq;
	uint8_t mode;
	uint8_t weekly_sel;
	uint8_t tz_set;
	uint8_t bright_pct;
	int32_t tz_min;
	char ssid[CFG_SSID_MAX];
	char psk[CFG_PSK_MAX];
	char token[CFG_TOKEN_MAX];
	char ap_psk[CFG_AP_PSK_MAX];
	uint32_t crc;
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
	if (out->magic == REC_MAGIC && out->crc == rec_crc(out)) {
		return true;
	}

	/*
	 * The 1.3.4 layout: the front of the current one, sealed by crc_134.
	 *
	 * Read straight out of `buf` rather than through a struct of its own,
	 * because it IS the current struct's prefix -- and because this
	 * function already holds four record-sized locals on the main stack.
	 * The theme and Focus fields after it are whatever was on flash (a
	 * 1.3.4 record pads with zeros, the sector beyond reads erased), so
	 * they are zeroed: Dark, Focus off.
	 */
	{
		uint32_t seal;

		memcpy(&seal, buf + offsetof(struct rec, crc_134), sizeof(seal));
		if (out->magic == REC_MAGIC &&
		    seal == crc32_ieee(buf, offsetof(struct rec, crc_134))) {
			memset((uint8_t *)out + offsetof(struct rec, crc_134), 0,
			       sizeof(*out) - offsetof(struct rec, crc_134));
			out->crc = rec_crc(out);
			printk("[cfg] migrated 1.3.4 record (seq %u)\n", out->seq);
			return true;
		}
	}

	/* Not a current record -- try the layout from before the edition latch. */
	struct rec_pre_lock pl;

	memcpy(&pl, buf, sizeof(pl));
	if (pl.magic == REC_MAGIC &&
	    pl.crc == crc32_ieee((const uint8_t *)&pl,
				 offsetof(struct rec_pre_lock, crc))) {
		memset(out, 0, sizeof(*out));
		out->magic = pl.magic;
		out->seq = pl.seq;
		out->mode = pl.mode;
		out->weekly_sel = pl.weekly_sel;
		out->tz_set = pl.tz_set;
		out->bright_pct = pl.bright_pct;
		out->tz_min = pl.tz_min;
		memcpy(out->ssid, pl.ssid, sizeof(out->ssid));
		memcpy(out->psk, pl.psk, sizeof(out->psk));
		memcpy(out->token, pl.token, sizeof(out->token));
		memcpy(out->ap_psk, pl.ap_psk, sizeof(out->ap_psk));
		out->ota_state = pl.ota_state;
		memcpy(out->ota_target, pl.ota_target, sizeof(out->ota_target));
		out->main_src = pl.main_src;
		out->edition = pl.edition;
		/*
		 * Latched, whatever it says.
		 *
		 * This used to read a 0 as "never written" and leave the
		 * record open, on the argument that 0 is both "Claude" and
		 * "blank" and an unprovisioned unit should stay provisionable.
		 * That reading had a cost the argument left out: every unit
		 * already in the field carries a pre-lock record, so the
		 * update that introduced the latch would have delivered every
		 * one of them UNLATCHED -- and its owner, holding the same
		 * binary the factory uses, could have stamped it Codex. The
		 * latch exists to stop exactly that.
		 *
		 * The trade is that a genuinely blank unit on old firmware
		 * arrives latched as Claude. Only Claude units have ever
		 * shipped, so that is the right answer for every fielded
		 * record; a bench board that was meant to become Codex needs
		 * its config partition erased first, which is the documented
		 * factory operation for changing a stamp.
		 */
		out->edition_locked = 1;
		out->crc = rec_crc(out);
		printk("[cfg] migrated pre-lock record (seq %u, edition %u, locked)\n",
		       pl.seq, pl.edition);
		return true;
	}

	/* Older -- the layout from before `edition` itself. */
	struct rec_pre_edition pe;

	memcpy(&pe, buf, sizeof(pe));
	if (pe.magic == REC_MAGIC &&
	    pe.crc == crc32_ieee((const uint8_t *)&pe,
				 offsetof(struct rec_pre_edition, crc))) {
		memset(out, 0, sizeof(*out));
		out->magic = pe.magic;
		out->seq = pe.seq;
		out->mode = pe.mode;
		out->weekly_sel = pe.weekly_sel;
		out->tz_set = pe.tz_set;
		out->bright_pct = pe.bright_pct;
		out->tz_min = pe.tz_min;
		memcpy(out->ssid, pe.ssid, sizeof(out->ssid));
		memcpy(out->psk, pe.psk, sizeof(out->psk));
		memcpy(out->token, pe.token, sizeof(out->token));
		memcpy(out->ap_psk, pe.ap_psk, sizeof(out->ap_psk));
		out->ota_state = pe.ota_state;
		memcpy(out->ota_target, pe.ota_target, sizeof(out->ota_target));
		out->main_src = pe.main_src;
		out->edition = 0;	/* unset -> Claude, what shipped */
		/* Latched as Claude, for the reason given in the pre-lock
		 * branch above: this record layout is what every fielded unit
		 * has, and all of them are Claude units. */
		out->edition_locked = 1;
		out->crc = rec_crc(out);
		printk("[cfg] migrated pre-edition record (seq %u, locked)\n",
		       pe.seq);
		return true;
	}

	/* Older -- the layout from before main_src. */
	struct rec_pre_src prev;

	memcpy(&prev, buf, sizeof(prev));
	if (prev.magic == REC_MAGIC &&
	    prev.crc == crc32_ieee((const uint8_t *)&prev,
				   offsetof(struct rec_pre_src, crc))) {
		memset(out, 0, sizeof(*out));
		out->magic = prev.magic;
		out->seq = prev.seq;
		out->mode = prev.mode;
		out->weekly_sel = prev.weekly_sel;
		out->tz_set = prev.tz_set;
		out->bright_pct = prev.bright_pct;
		out->tz_min = prev.tz_min;
		memcpy(out->ssid, prev.ssid, sizeof(out->ssid));
		memcpy(out->psk, prev.psk, sizeof(out->psk));
		memcpy(out->token, prev.token, sizeof(out->token));
		memcpy(out->ap_psk, prev.ap_psk, sizeof(out->ap_psk));
		out->ota_state = prev.ota_state;
		memcpy(out->ota_target, prev.ota_target, sizeof(out->ota_target));
		out->main_src = 0;	/* unset -> the default, not garbage */
		out->crc = rec_crc(out);
		printk("[cfg] migrated pre-main_src record (seq %u)\n", prev.seq);
		return true;
	}

	/* Older still -- the pre-OTA layout. */
	struct rec_legacy old;

	memcpy(&old, buf, sizeof(old));
	if (old.magic != REC_MAGIC ||
	    old.crc != crc32_ieee((const uint8_t *)&old,
				  offsetof(struct rec_legacy, crc))) {
		return false;
	}
	memset(out, 0, sizeof(*out));
	out->magic = old.magic;
	out->seq = old.seq;
	out->mode = old.mode;
	out->weekly_sel = old.weekly_sel;
	out->tz_set = old.tz_set;
	out->bright_pct = old.bright_pct;
	out->tz_min = old.tz_min;
	memcpy(out->ssid, old.ssid, sizeof(out->ssid));
	memcpy(out->psk, old.psk, sizeof(out->psk));
	memcpy(out->token, old.token, sizeof(out->token));
	memcpy(out->ap_psk, old.ap_psk, sizeof(out->ap_psk));
	out->crc = rec_crc(out);	/* valid in RAM; resealed on next persist */
	printk("[cfg] migrated pre-OTA record (seq %u)\n", old.seq);
	return true;
}

/* Called with cfg_lock held. */
static int persist(void)
{
	static uint8_t buf[REC_WIRE];
	int next = (cur_slot == SLOT_A) ? SLOT_B : SLOT_A;

	cfg.magic = REC_MAGIC;
	cfg.seq++;
	/* Both seals, inner first: the outer CRC covers the inner one. */
	cfg.crc_134 = crc32_ieee((const uint8_t *)&cfg,
				 offsetof(struct rec, crc_134));
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

/* Is this slot a whole record of THIS layout, outer seal and all -- not one
 * slot_load() could only read by migrating it? */
static bool slot_whole(int off)
{
	/* On the stack, not static: this runs once, at boot, after
	 * slot_load()'s larger frames have returned, so it reuses their room
	 * instead of holding 507 bytes of RAM for the life of the board. */
	uint8_t buf[sizeof(struct rec)];
	uint32_t magic, crc;

	if (flash_area_read(fa, off, buf, sizeof(buf)) != 0) {
		return false;
	}
	memcpy(&magic, buf + offsetof(struct rec, magic), sizeof(magic));
	memcpy(&crc, buf + offsetof(struct rec, crc), sizeof(crc));
	return magic == REC_MAGIC &&
	       crc == crc32_ieee(buf, offsetof(struct rec, crc));
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
	const struct rec *other = NULL;

	if (va && (!vb || a.seq >= b.seq)) {
		cfg = a;
		cur_slot = SLOT_A;
		other = vb ? &b : NULL;
	} else if (vb) {
		cfg = b;
		cur_slot = SLOT_B;
		other = va ? &a : NULL;
	} else {
		memset(&cfg, 0, sizeof(cfg));
		cur_slot = -1;	/* first persist() lands in slot A */
	}

	/*
	 * The newer slot can be whole as 1.3.4 reads it and torn after that.
	 *
	 * A record is written front to back, and 1.3.4's seal sits 32 bytes
	 * before the end. Power lost inside those 32 bytes leaves a slot that
	 * passes the 1.3.4 check with the newer sequence -- so it wins, and on
	 * its own it says Dark and Focus off. The same happens, by design,
	 * after a downgrade: 1.3.4 writes its record and nothing after it.
	 * Either way the other slot can still be a whole record of ours, and
	 * the theme the owner had is sealed in it. Take the appended fields
	 * from there; everything 1.3.4 knows about stays the newer slot's.
	 * tests/cfg_store cuts a write at every one of its 513 byte counts.
	 *
	 * After a downgrade this rescue holds for ONE write by 1.3.4, not
	 * two, and support should expect exactly that. 1.3.4 alternates slots
	 * like this file does: its first write lands on the slot it did not
	 * boot from and leaves our whole record in the other, so an update
	 * back brings the theme and Focus with it ("re-update" in
	 * tests/cfg_store). Its second write lands on that other slot, and
	 * then neither holds our fields: the update comes back on Dark with
	 * Focus off, every 1.3.4 setting intact ("re-update 2").
	 *
	 * What counts as a write, read off v1.3.4's call sites (USB mode; its
	 * token and timezone writes are WiFi-mode only, and release.sh has
	 * never shipped a WiFi build): a brightness step, a tap on the weekly
	 * peek row, an edition stamp, cfg_set_ota_state(1) just before it
	 * installs an OTA (proto.c:616), and cfg_set_ota_state(0) on the first
	 * boot after one (main.c:278). So an OTA back to this firmware spends
	 * the one write by itself:
	 *   - downgraded by flashing, nothing touched, then OTA back: the
	 *     theme returns;
	 *   - downgraded by flashing, brightness or the weekly row changed
	 *     once, then OTA back: Dark, Focus off;
	 *   - downgraded by OTA: 1.3.4's first boot has already written once,
	 *     so the OTA back loses them with nobody touching anything.
	 * Found by the final review of the theme work (N4, 2026-10-27) and
	 * corrected against the 1.3.4 source in its review; nothing 1.3.4
	 * itself stores is lost in any of these.
	 */
	if (other != NULL && cur_slot >= 0 && !slot_whole(cur_slot) &&
	    slot_whole(cur_slot == SLOT_A ? SLOT_B : SLOT_A)) {
		size_t from = offsetof(struct rec, theme_id);

		memcpy((uint8_t *)&cfg + from, (const uint8_t *)other + from,
		       offsetof(struct rec, crc) - from);
		cfg.crc = rec_crc(&cfg);	/* in RAM; resealed on next write */
		printk("[cfg] theme and focus taken from the older slot\n");
	}

	/* Never log the token itself, only whether we have one -- and the
	 * same for the SSID. This line used to print the stored network name
	 * verbatim, and the daemon copies the boot lines into bridge.log,
	 * which is the support file customers send us: a unit set up for
	 * WiFi before 2026-10-27 still carries its owner's network name in
	 * the record, and the name of somebody's home network is theirs, not
	 * ours to collect. WiFi is gone from the image, so nothing reads it
	 * now beyond whether it is there (review NICE-2, 2026-10-28). */
	printk("[cfg] mode=%d ssid=%s token=%s\n", cfg.mode,
	       cfg.ssid[0] ? "present" : "(none)",
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

uint8_t cfg_get_main_src(void)
{
	return cfg.main_src;
}

int cfg_set_main_src(uint8_t src)
{
	int rc;

	k_mutex_lock(&cfg_lock, K_FOREVER);
	cfg.main_src = src;
	rc = persist();
	k_mutex_unlock(&cfg_lock);
	return rc;
}

uint8_t cfg_get_edition(void)
{
	return cfg.edition;
}

bool cfg_edition_locked(void)
{
	return cfg.edition_locked != 0;
}

/*
 * Stamp the edition, once, for the life of the record. See cfg_store.h.
 *
 * The range check is here rather than only at the protocol edge because this
 * write cannot be taken back: a value this firmware does not recognise would
 * latch, and bootclip_active() would then fall back to Claude forever on a
 * board that is physically a Codex unit.
 */
int cfg_set_edition(uint8_t edition)
{
	int rc;

	if (edition != CFG_EDITION_CLAUDE && edition != CFG_EDITION_CODEX) {
		return -EINVAL;
	}

	k_mutex_lock(&cfg_lock, K_FOREVER);
	if (cfg.edition_locked) {
		k_mutex_unlock(&cfg_lock);
		return -EPERM;
	}
	uint8_t was_edition = cfg.edition;

	cfg.edition = edition;
	cfg.edition_locked = 1;
	rc = persist();
	if (rc != 0) {
		/*
		 * The flash write failed, so nothing was latched on the device
		 * -- and the RAM mirror must not claim otherwise, or a retry
		 * on this same boot would be refused for a stamp that does not
		 * exist.
		 *
		 * BOTH fields, not just the latch. persist() serialises the
		 * whole struct, so leaving cfg.edition set meant the next
		 * unrelated successful write -- a brightness change is one
		 * line away, backlight.c -> cfg_set_bright_pct -> persist() --
		 * sealed the new edition WITHOUT its lock. The unit would then
		 * play the other clip while cfg_edition_locked() reported
		 * false, so anyone with the cable could stamp it again: the
		 * exact hole the latch exists to close, reached by a path
		 * nobody would think to test.
		 */
		cfg.edition = was_edition;
		cfg.edition_locked = 0;
	}
	k_mutex_unlock(&cfg_lock);
	return rc;
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

uint8_t cfg_get_ota_state(char *ver, size_t len)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	if (ver && len) {
		strncpy(ver, cfg.ota_target, len - 1);
		ver[len - 1] = '\0';
	}
	uint8_t st = cfg.ota_state;

	k_mutex_unlock(&cfg_lock);
	return st;
}

int cfg_set_ota_state(uint8_t st, const char *ver)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	cfg.ota_state = st;
	memset(cfg.ota_target, 0, sizeof(cfg.ota_target));
	if (ver) {
		strncpy(cfg.ota_target, ver, sizeof(cfg.ota_target) - 1);
	}
	int rc = persist();

	k_mutex_unlock(&cfg_lock);
	return rc;
}

int cfg_reset(void)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);

	/*
	 * The edition survives a factory reset, and so does its latch.
	 *
	 * A reset wipes what the USER put on the device -- network, token,
	 * brightness, timezone. The edition is not that. It is a property of
	 * the enclosure the board is screwed into, decided once at the factory,
	 * and wiping it here would have made the settings menu a second route
	 * to changing it: reset, then re-provision, and a Codex unit is playing
	 * the Claude clip inside a Codex box. No cable, no CLI, two taps.
	 *
	 * Carried through the memset rather than written back after, so there
	 * is no window where the record on flash is unstamped.
	 */
	uint8_t ed = cfg.edition;
	uint8_t locked = cfg.edition_locked;

	memset(&cfg, 0, sizeof(cfg));
	cfg.edition = ed;
	cfg.edition_locked = locked;

	flash_area_erase(fa, SLOT_A, SECTOR);
	flash_area_erase(fa, SLOT_B, SECTOR);
	cur_slot = -1;
	/*
	 * Both slots are blank now, so the stamp exists only in RAM until
	 * something persists it. Write it back immediately: a reset is
	 * followed by a reboot, and a power cut in that gap would take the
	 * edition with it.
	 */
	if (locked) {
		persist();
	}
	k_mutex_unlock(&cfg_lock);
	return 0;
}

/* Called with cfg_lock held. */
static void theme_put(const struct cfg_theme *t)
{
	cfg.theme_id = t->id;
	memcpy(cfg.theme_rgb, t->rgb, sizeof(cfg.theme_rgb));
	cfg.theme_w = t->w;
	cfg.theme_ends = t->ends;
	cfg.theme_dot = t->dot;
	cfg.theme_by = t->by;
}

/* Called with cfg_lock held. */
static void theme_take(struct cfg_theme *out)
{
	out->id = cfg.theme_id;
	memcpy(out->rgb, cfg.theme_rgb, sizeof(out->rgb));
	out->w = cfg.theme_w;
	out->ends = cfg.theme_ends;
	out->dot = cfg.theme_dot;
	out->by = cfg.theme_by;
}

void cfg_get_theme(struct cfg_theme *out)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	theme_take(out);
	k_mutex_unlock(&cfg_lock);
}

/*
 * persist() writes the RAM record, so the new values go into it first -- and
 * on a failed write they come back OUT. RAM is what everything else believes:
 * theme_now reports it, ui_theme.c shows it as "stored", and the "nothing
 * changed" check below compares against it. Left holding an unstored theme,
 * the board told the daemon on its next greeting that it held a theme a
 * reboot would take away, and a retry of the same theme hit the short-circuit
 * and returned success with nothing written (review of 1cb4fd6, M1;
 * tests/cfg_store plants a flash that refuses erases). cfg_set_focus does the
 * same. seq is left advanced: a gap in it costs nothing.
 */
int cfg_set_theme(const struct cfg_theme *t)
{
	struct cfg_theme was;
	int rc;

	k_mutex_lock(&cfg_lock, K_FOREVER);
	/* Nothing changed, nothing written: a flash sector erased for the
	 * same 26 bytes is wear and a power-cut window bought for nothing. */
	if (cfg.theme_id == t->id &&
	    memcmp(cfg.theme_rgb, t->rgb, sizeof(cfg.theme_rgb)) == 0 &&
	    cfg.theme_w == t->w && cfg.theme_ends == t->ends &&
	    cfg.theme_dot == t->dot && cfg.theme_by == t->by) {
		k_mutex_unlock(&cfg_lock);
		return 0;
	}
	theme_take(&was);
	theme_put(t);
	rc = persist();
	if (rc != 0) {
		theme_put(&was);
	}
	k_mutex_unlock(&cfg_lock);
	return rc;
}

/*
 * Only 1 means on, and only 1 means weekly. Anything else on flash -- a value
 * a later firmware gave a third window, read back after a downgrade, or a
 * flipped bit -- reads as the default, because Settings can only show Off,
 * Session and Weekly and a panel stuck in a mode its own menu cannot name has
 * no way back. tests/cfg_store plants such bytes.
 */
uint8_t cfg_get_focus_on(void)
{
	return cfg.focus_on == 1 ? 1 : 0;
}

uint8_t cfg_get_focus_win(void)
{
	return cfg.focus_win == 1 ? 1 : 0;
}

int cfg_set_focus(uint8_t on, uint8_t win)
{
	uint8_t was_on, was_win;
	int rc;

	if (on > 1 || win > 1) {
		return -EINVAL;
	}
	k_mutex_lock(&cfg_lock, K_FOREVER);
	/* The same short-circuit as cfg_set_theme: a tap on the Focus button
	 * that is already lit used to erase a sector to store the two bytes
	 * it already held (final review N2, 2026-10-27). The comparison is on
	 * the RAW bytes, not cfg_get_focus_*(): a stray byte read back as the
	 * default (see above) still differs from the 0 being chosen, so that
	 * choice is written and the stray byte is cleaned off flash. */
	if (cfg.focus_on == on && cfg.focus_win == win) {
		k_mutex_unlock(&cfg_lock);
		return 0;
	}
	was_on = cfg.focus_on;
	was_win = cfg.focus_win;
	cfg.focus_on = on;
	cfg.focus_win = win;
	rc = persist();
	if (rc != 0) {
		/* Put back, as cfg_set_theme does, and for the same reason. */
		cfg.focus_on = was_on;
		cfg.focus_win = was_win;
	}
	k_mutex_unlock(&cfg_lock);
	return rc;
}
