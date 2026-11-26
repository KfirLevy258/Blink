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

/*
 * The token's last four bytes are quiet hours (issue #17).
 *
 * The record had no spare byte inside any seal, and a new field appended
 * behind crc would have needed a fourth seal and a fourth reader, for four
 * bytes. The token is dead weight: no firmware since 2026-10-27 reads one
 * except to erase it at boot (main.c), and release.sh never shipped the
 * WiFi build that wrote one, so every board in the field holds an empty
 * token -- zeros -- there. Zeros read as quiet hours off, 22:00 to 07:00,
 * so an update from any older layout arrives with quiet hours off and
 * nothing else moved. The bytes sit inside 1.3.4's seal, so a downgrade to
 * 1.3.4 or 1.4.x keeps them sealed and writes them back unchanged: those
 * images see a token that is empty (or, with quiet hours set, still empty
 * -- token[0] is 0), and they too only ever erase one.
 *
 * CFG_TOKEN_MAX stays 320 because the frozen legacy layouts below, and
 * tests/cfg_store's copies of them, are sized by it. A legacy token longer
 * than 315 characters loses its tail on the way in; nothing reads a token,
 * so nothing is lost that anything could use. The _Static_asserts after
 * the struct pin every offset that was already on flash.
 */
#define REC_TOKEN_LEN (CFG_TOKEN_MAX - 4)

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
	char token[REC_TOKEN_LEN];
	uint8_t quiet_on;		/* only 1 means on; see cfg_get_quiet */
	uint8_t quiet_from;		/* half-hour index + 1, 1..48; 0 = unset */
	uint8_t quiet_until;		/* the same */
	uint8_t quiet_spare;		/* always 0 */
	/*
	 * The setup AP's password, one byte shorter than it was, and the panel.
	 *
	 * Which display module the board drives (issue #23) needed a byte, and
	 * the record had no spare one that every older firmware would carry
	 * through untouched -- except this. Up to 2026-11-25 the field was 17
	 * bytes, `CFG_AP_PSK_MAX` = 16 characters + NUL, and every writer that
	 * ever touched it kept the 17th byte a NUL: cfg_set_ap_psk() copies at
	 * most 16 characters and then stores '\0' at [16], the generated
	 * passwords are 10 characters, cfg_reset() and a blank record zero it,
	 * and each migration below copies it from a layout whose own setter did
	 * the same. So on every record any shipped firmware wrote, this byte
	 * reads 0, which is CFG_PANEL_STANDARD -- what every desk already runs.
	 *
	 * It sits inside the crc_134 prefix, so all three seals cover it and
	 * nothing needs a new reader. A downgrade to 1.3.4 or 1.4.x keeps it,
	 * because those firmwares rewrite their whole RAM copy of the prefix,
	 * this byte included, and so a re-update finds it where it was. The one
	 * way an older image clears it is its own cfg_set_ap_psk(), which
	 * stores '\0' at [16]. Nothing in this tree calls that setter any more
	 * (the setup AP left with the WiFi mode on 2026-10-27), and an old
	 * image calls it only to generate a password when none is stored, so
	 * an alternate board downgraded to one and back could at worst come
	 * back standard -- mirrored, fixed by one `blink config panel
	 * alternate` (not yet measured on a desk). An old image reads the byte
	 * as part of a password it no longer uses; a 16-character password next
	 * to an alternate byte would read there as 17 characters and no NUL,
	 * which its strncpy into a bounded buffer still stops at (generated
	 * passwords are 10 characters, so no such record is known to exist).
	 *
	 * Every other offset, and sizeof(struct rec), are unchanged; the
	 * BUILD_ASSERTs after the struct and tests/cfg_store hold them.
	 */
	char ap_psk[CFG_AP_PSK_MAX - 1];
	uint8_t panel;			/* enum cfg_panel; see cfg_get_panel */
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
	/*
	 * 1.4.x's seal, kept where 1.4.x looks for it -- the same move crc_134
	 * made, one layout later (spec section 5.2).
	 *
	 * Everything above this line is the 1.4.x record, and this is its CRC
	 * over exactly those bytes. Without it a downgrade from a WiFi build
	 * found `crc` in the wrong place, fell back to the crc_134 reader and
	 * zeroed the theme and Focus; with it, 1.4.x keeps them and loses only
	 * the pairing, which it cannot use anyway. Nothing after this line is
	 * ever copied from the other slot (D44, see cfg_init): a pairing comes
	 * from one whole write or not at all.
	 */
	uint32_t crc_143;
	uint8_t pair_set;		/* 1 = WiFi paired; anything else = not (D28) */
	uint8_t pair_key[CFG_PAIR_KEY_LEN];	/* secret, never logged */
	uint32_t crc;		/* over everything above, always last */
} __packed;

/*
 * The layout every shipped firmware and every downgrade reads, pinned. Two
 * fields were carved out of bytes that older firmware never used: quiet hours
 * out of the token's last four, and the panel out of ap_psk's last byte. They
 * must not move anything else: 1.3.4 finds its seal at 471, 1.4.x at 503, and
 * a record of any other length is a record nobody can read. Counted from the
 * base layout (5f6b635) by hand and again by tests/cfg_store. Plain
 * _Static_assert rather than Zephyr's BUILD_ASSERT, so the host stubs need
 * nothing new.
 */
_Static_assert(offsetof(struct rec, quiet_on) == 114 + 316,
	       "quiet hours sit in the token's last four bytes");
_Static_assert(offsetof(struct rec, ap_psk) == 434, "ap_psk moved");
_Static_assert(offsetof(struct rec, panel) == 450, "panel is ap_psk's 17th byte");
_Static_assert(offsetof(struct rec, ota_state) == 451, "ota_state moved");
_Static_assert(offsetof(struct rec, crc_134) == 471, "1.3.4's seal moved");
_Static_assert(offsetof(struct rec, theme_id) == 475, "theme_id moved");
_Static_assert(offsetof(struct rec, focus_on) == 501, "Focus moved");
_Static_assert(offsetof(struct rec, crc_143) == 503, "1.4.x's seal moved");
_Static_assert(offsetof(struct rec, pair_set) == 507, "pair_set moved");
_Static_assert(offsetof(struct rec, crc) == 540, "crc moved");
_Static_assert(sizeof(struct rec) == 544, "the record changed size");

/* Which seal slot_load() read a record through: decides what cfg_init may
 * take from the other slot. The layouts older than 1.3.4 count as 1.3.4 --
 * they hold no theme either. */
enum rec_layout { LAYOUT_134, LAYOUT_143, LAYOUT_WIFI };

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

static bool slot_load(int off, struct rec *out, enum rec_layout *layout)
{
	uint8_t buf[REC_WIRE];

	*layout = LAYOUT_134;
	if (flash_area_read(fa, off, buf, sizeof(buf)) != 0) {
		return false;
	}
	memcpy(out, buf, sizeof(*out));
	if (out->magic == REC_MAGIC && out->crc == rec_crc(out)) {
		*layout = LAYOUT_WIFI;
		return true;
	}

	/*
	 * The 1.4.x layout, sealed by crc_143: written by 1.4.x after a
	 * downgrade, or ours torn inside the pairing tail. The theme and Focus
	 * are sealed and kept; the tail is not, so the board is unpaired --
	 * the safe side (D21), whichever of the two it was.
	 */
	{
		uint32_t seal;

		memcpy(&seal, buf + offsetof(struct rec, crc_143), sizeof(seal));
		if (out->magic == REC_MAGIC &&
		    seal == crc32_ieee(buf, offsetof(struct rec, crc_143))) {
			memset((uint8_t *)out + offsetof(struct rec, pair_set), 0,
			       sizeof(*out) - offsetof(struct rec, pair_set));
			out->crc = rec_crc(out);
			*layout = LAYOUT_143;
			printk("[cfg] migrated 1.4.x record (seq %u)\n", out->seq);
			return true;
		}
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
	 *
	 * Quiet hours are not zeroed here: they are inside this seal, the
	 * last four bytes of what 1.3.4 calls its token. A record 1.3.4
	 * wrote holds zeros there (off), and one 1.3.4 wrote back after a
	 * downgrade holds whatever we left, sealed -- the owner's schedule.
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
		/* The destination's size: 316 of the old 320 token bytes. The
		 * last four would land on quiet hours, which stay the memset's
		 * zeros -- off -- as they do in every reader below. */
		memcpy(out->token, pl.token, sizeof(out->token));
		/* sizeof(out->ap_psk), 16 bytes, off a 17-byte field: the 17th
		 * was its NUL, and out->panel stays the memset's 0 = standard. */
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
		memcpy(out->ap_psk, pe.ap_psk, sizeof(out->ap_psk));	/* 16 of 17 */
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
		memcpy(out->ap_psk, prev.ap_psk, sizeof(out->ap_psk));	/* 16 of 17 */
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
	memcpy(out->ap_psk, old.ap_psk, sizeof(out->ap_psk));	/* 16 of 17 */
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
	/* Every seal, inner first: each outer CRC covers the inner ones. */
	cfg.crc_134 = crc32_ieee((const uint8_t *)&cfg,
				 offsetof(struct rec, crc_134));
	cfg.crc_143 = crc32_ieee((const uint8_t *)&cfg,
				 offsetof(struct rec, crc_143));
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
	enum rec_layout la, lb, cur_layout = LAYOUT_134, other_layout = LAYOUT_134;
	bool va = slot_load(SLOT_A, &a, &la);
	bool vb = slot_load(SLOT_B, &b, &lb);
	const struct rec *other = NULL;

	if (va && (!vb || a.seq >= b.seq)) {
		cfg = a;
		cur_slot = SLOT_A;
		cur_layout = la;
		other = vb ? &b : NULL;
		other_layout = lb;
	} else if (vb) {
		cfg = b;
		cur_slot = SLOT_B;
		cur_layout = lb;
		other = va ? &a : NULL;
		other_layout = la;
	} else {
		memset(&cfg, 0, sizeof(cfg));
		cur_slot = -1;	/* first persist() lands in slot A */
	}

	/*
	 * The newer slot can be whole as 1.3.4 reads it and torn after that.
	 *
	 * A record is written front to back, and 1.3.4's seal sits 69 bytes
	 * before the end. Power lost inside those 69 bytes leaves a slot that
	 * passes the 1.3.4 check with the newer sequence -- so it wins, and on
	 * its own it says Dark and Focus off. The same happens, by design,
	 * after a downgrade: 1.3.4 writes its record and nothing after it.
	 * Either way the other slot can still be a whole record of ours, and
	 * the theme the owner had is sealed in it. Take the appended fields
	 * from there; everything 1.3.4 knows about stays the newer slot's.
	 * tests/cfg_store cuts a write at every one of its 545 byte counts.
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
	 *
	 * Layout-aware since the pairing tail (D44). Copying everything up to
	 * `crc`, as this did, would have carried pair_set and pair_key too: a
	 * board downgraded to 1.4.x, written there and updated again would
	 * have had 1.4.x's newer theme overwritten by the older slot's and its
	 * revoked pairing brought back; and a torn pairing write whose 1.3.4
	 * prefix (the new SSID and password) survived would have been stitched
	 * to the older slot's key. So only a record read through crc_134 takes
	 * anything, only theme_id to focus_win, and only from a slot whose
	 * seal covers them. A record read through crc_143 takes nothing: that
	 * write was newer than the other slot, and a pairing a 1.4.x image
	 * could not see is not brought back. The pairing tail never moves.
	 */
	if (other != NULL && cur_layout == LAYOUT_134 &&
	    other_layout != LAYOUT_134) {
		size_t from = offsetof(struct rec, theme_id);

		memcpy((uint8_t *)&cfg + from, (const uint8_t *)other + from,
		       offsetof(struct rec, crc_143) - from);
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
	/*
	 * Bounded by the field, not only by the caller's buffer. main.c asks
	 * with a CFG_TOKEN_MAX buffer, four bytes more than the field now
	 * holds, and a token read off an older layout need not have a NUL
	 * inside the field's 316 bytes: a plain strncpy(len - 1) would then run
	 * on into the quiet-hours bytes and the setup AP's password.
	 */
	size_t n = strnlen(cfg.token, sizeof(cfg.token));

	if (n > len - 1) {
		n = len - 1;
	}
	memcpy(tok, cfg.token, n);
	tok[n] = '\0';
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

int cfg_set_pairing(const uint8_t *ssid, size_t ssid_len, const char *psk,
		    const uint8_t key[CFG_PAIR_KEY_LEN])
{
	char was_ssid[CFG_SSID_MAX], was_psk[CFG_PSK_MAX];
	uint8_t was_key[CFG_PAIR_KEY_LEN], was_set;
	int rc;

	if (ssid == NULL || ssid_len == 0 || ssid_len > CFG_SSID_MAX - 1 ||
	    memchr(ssid, 0x00, ssid_len) != NULL || psk == NULL ||
	    strnlen(psk, CFG_PSK_MAX) > CFG_PSK_MAX - 1 || key == NULL) {
		return -EINVAL;
	}
	k_mutex_lock(&cfg_lock, K_FOREVER);
	memcpy(was_ssid, cfg.ssid, sizeof(was_ssid));
	memcpy(was_psk, cfg.psk, sizeof(was_psk));
	memcpy(was_key, cfg.pair_key, sizeof(was_key));
	was_set = cfg.pair_set;

	memset(cfg.ssid, 0, sizeof(cfg.ssid));
	memcpy(cfg.ssid, ssid, ssid_len);
	memset(cfg.psk, 0, sizeof(cfg.psk));
	strcpy(cfg.psk, psk);	/* length checked above */
	memcpy(cfg.pair_key, key, sizeof(cfg.pair_key));
	cfg.pair_set = 1;
	rc = persist();
	if (rc != 0) {
		/* Put back, as cfg_set_theme does: RAM is what the board acts
		 * on, and it must not claim a pairing a reboot would drop. */
		memcpy(cfg.ssid, was_ssid, sizeof(cfg.ssid));
		memcpy(cfg.psk, was_psk, sizeof(cfg.psk));
		memcpy(cfg.pair_key, was_key, sizeof(cfg.pair_key));
		cfg.pair_set = was_set;
	}
	k_mutex_unlock(&cfg_lock);
	/* The old secrets do not outlive this frame on the stack. */
	memset(was_psk, 0, sizeof(was_psk));
	memset(was_key, 0, sizeof(was_key));
	return rc;
}

bool cfg_get_pairing(uint8_t ssid[32], size_t *ssid_len, char *psk,
		     size_t psk_len, uint8_t key[CFG_PAIR_KEY_LEN])
{
	bool ok;

	k_mutex_lock(&cfg_lock, K_FOREVER);
	/* A password cut to fit the buffer would fail to join with no word
	 * said; refusing it is the bug a caller can see. */
	ok = cfg.pair_set == 1 && psk_len > strnlen(cfg.psk, CFG_PSK_MAX - 1);
	if (ok) {
		*ssid_len = strnlen(cfg.ssid, CFG_SSID_MAX - 1);
		memcpy(ssid, cfg.ssid, *ssid_len);
		strncpy(psk, cfg.psk, psk_len);
		psk[psk_len - 1] = '\0';
		memcpy(key, cfg.pair_key, CFG_PAIR_KEY_LEN);
	}
	k_mutex_unlock(&cfg_lock);
	return ok;
}

bool cfg_paired(void)
{
	return cfg.pair_set == 1;
}

/*
 * Forget WiFi (D33). persist() writes the next slot and leaves the previous
 * one intact on purpose, so one cleared write would leave the password and
 * key in the other sector for whoever reads the flash. Two persists, then:
 * the first writes slot N, the second erases the other slot -- the one still
 * holding them -- and writes it too, seq one higher. Only then is it 0. RAM
 * is cleared first and stays cleared on any failure, so the board stops using
 * the network either way. The edition latch is in the 1.3.4 prefix, carried
 * by both writes.
 */
int cfg_clear_pairing(void)
{
	int rc;

	k_mutex_lock(&cfg_lock, K_FOREVER);
	memset(cfg.ssid, 0, sizeof(cfg.ssid));
	memset(cfg.psk, 0, sizeof(cfg.psk));
	memset(cfg.pair_key, 0, sizeof(cfg.pair_key));
	cfg.pair_set = 0;
	rc = persist();
	if (rc == 0) {
		rc = persist();
	}
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

/*
 * Only 1 is alternate. Anything else -- the 0 every shipped record carries in
 * this byte (see struct rec), or a stray value -- is the standard panel, the
 * one every desk in the field has.
 */
uint8_t cfg_get_panel(void)
{
	return cfg.panel == CFG_PANEL_ALTERNATE ? CFG_PANEL_ALTERNATE
						: CFG_PANEL_STANDARD;
}

/*
 * Rolled back on a failed write, as cfg_set_edition does and for the same
 * reason: persist() serialises the whole RAM record, so a value left behind in
 * RAM would be sealed by the next unrelated write -- a brightness step -- and
 * the board would change panel at its next boot with nobody having asked.
 */
int cfg_set_panel(uint8_t panel)
{
	uint8_t was;
	int rc;

	if (panel != CFG_PANEL_STANDARD && panel != CFG_PANEL_ALTERNATE) {
		return -EINVAL;
	}
	k_mutex_lock(&cfg_lock, K_FOREVER);
	was = cfg.panel;
	cfg.panel = panel;
	rc = persist();
	if (rc != 0) {
		cfg.panel = was;
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

/*
 * The field is 16 bytes now and its old NUL is the panel byte, so a record an
 * older firmware wrote can hold 16 characters with no terminator inside the
 * array. Read with strnlen over the array, never strncpy off its end, and
 * always terminate the caller's copy.
 */
bool cfg_get_ap_psk(char *psk, size_t len)
{
	size_t n;

	if (psk == NULL || len == 0) {
		return false;
	}
	k_mutex_lock(&cfg_lock, K_FOREVER);
	n = strnlen(cfg.ap_psk, sizeof(cfg.ap_psk));
	if (n == 0) {
		k_mutex_unlock(&cfg_lock);
		return false;
	}
	if (n > len - 1) {
		n = len - 1;
	}
	memcpy(psk, cfg.ap_psk, n);
	psk[n] = '\0';
	k_mutex_unlock(&cfg_lock);
	return true;
}

/* At most 15 characters, so this firmware's own records keep a NUL inside the
 * field; generated passwords are 10. */
int cfg_set_ap_psk(const char *psk)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	memset(cfg.ap_psk, 0, sizeof(cfg.ap_psk));
	strncpy(cfg.ap_psk, psk ? psk : "", sizeof(cfg.ap_psk) - 1);

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
	 *
	 * The memset takes the WiFi pairing with it, and the two erases below
	 * take it off both sectors, as cfg_clear_pairing does. Quiet hours go
	 * with it too: zeros, which read as off, 22:00 to 07:00.
	 *
	 * The panel survives too, for the same kind of reason (issue #23). It
	 * says which display module is soldered to this board, a hardware fact
	 * like the edition, not a preference. And clearing it would be a trap:
	 * an alternate board reset from Settings would come back mirrored, with
	 * red and blue swapped, and the menu that could fix it cannot be
	 * operated on a mirrored screen -- every tap lands on the wrong side.
	 */
	uint8_t ed = cfg.edition;
	uint8_t locked = cfg.edition_locked;
	/* As cfg_get_panel reads it: a stray byte is not worth keeping. */
	uint8_t panel = cfg.panel == CFG_PANEL_ALTERNATE ? CFG_PANEL_ALTERNATE
							  : CFG_PANEL_STANDARD;

	memset(&cfg, 0, sizeof(cfg));
	cfg.edition = ed;
	cfg.edition_locked = locked;
	cfg.panel = panel;

	flash_area_erase(fa, SLOT_A, SECTOR);
	flash_area_erase(fa, SLOT_B, SECTOR);
	cur_slot = -1;
	/*
	 * Both slots are blank now, so the stamp exists only in RAM until
	 * something persists it. Write it back immediately: a reset is
	 * followed by a reboot, and a power cut in that gap would take the
	 * edition with it -- or, on an alternate board, the panel.
	 */
	if (locked || panel != CFG_PANEL_STANDARD) {
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

/*
 * Quiet hours (issue #17), in the token's last four bytes -- see struct rec.
 *
 * The times are stored as a half-hour index plus one, so that 0, which every
 * board in the field holds there, means "never set" rather than midnight.
 * Read the same way as Focus: only 1 means on, and a time of 0 or past 48 (a
 * later firmware's finer step after a downgrade, a flipped bit, the tail of a
 * long token an old WiFi build left) reads as the default rather than as a
 * time Settings' half-hour picker cannot show. tests/cfg_store plants such
 * bytes.
 */
#define QUIET_FROM_DEFAULT_MIN (22 * 60)
#define QUIET_UNTIL_DEFAULT_MIN (7 * 60)
#define QUIET_STEP_MIN 30
#define QUIET_SLOTS (24 * 60 / QUIET_STEP_MIN)	/* 48 */

static uint16_t quiet_min(uint8_t stored, uint16_t dflt)
{
	if (stored == 0 || stored > QUIET_SLOTS) {
		return dflt;
	}
	return (uint16_t)((stored - 1) * QUIET_STEP_MIN);
}

void cfg_get_quiet(uint8_t *on, uint16_t *from_min, uint16_t *until_min)
{
	k_mutex_lock(&cfg_lock, K_FOREVER);
	*on = cfg.quiet_on == 1 ? 1 : 0;
	*from_min = quiet_min(cfg.quiet_from, QUIET_FROM_DEFAULT_MIN);
	*until_min = quiet_min(cfg.quiet_until, QUIET_UNTIL_DEFAULT_MIN);
	k_mutex_unlock(&cfg_lock);
}

int cfg_set_quiet(uint8_t on, uint16_t from_min, uint16_t until_min)
{
	uint8_t was_on, was_from, was_until, from, until;
	int rc;

	if (on > 1 || from_min >= 24 * 60 || until_min >= 24 * 60 ||
	    from_min % QUIET_STEP_MIN != 0 || until_min % QUIET_STEP_MIN != 0) {
		return -EINVAL;
	}
	from = (uint8_t)(from_min / QUIET_STEP_MIN + 1);
	until = (uint8_t)(until_min / QUIET_STEP_MIN + 1);

	k_mutex_lock(&cfg_lock, K_FOREVER);
	/* cfg_set_focus's short-circuit, on the RAW bytes for its reason: a
	 * stray byte that reads as the default still differs from the value
	 * being chosen, so that choice is written and cleans it off flash.
	 * The same holds for a never-set 0 when the default is chosen: one
	 * write, after which the stored bytes say what the screen shows. */
	if (cfg.quiet_on == on && cfg.quiet_from == from &&
	    cfg.quiet_until == until) {
		k_mutex_unlock(&cfg_lock);
		return 0;
	}
	was_on = cfg.quiet_on;
	was_from = cfg.quiet_from;
	was_until = cfg.quiet_until;
	cfg.quiet_on = on;
	cfg.quiet_from = from;
	cfg.quiet_until = until;
	rc = persist();
	if (rc != 0) {
		/* Put back, as cfg_set_theme does, and for the same reason. */
		cfg.quiet_on = was_on;
		cfg.quiet_from = was_from;
		cfg.quiet_until = was_until;
	}
	k_mutex_unlock(&cfg_lock);
	return rc;
}
