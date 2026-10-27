/* The config record across an update and a downgrade, on a laptop.
 *
 * Build & run (tests/ci/check_host_tests.sh does this):
 *   cc -I firmware/src -I tests/cfg_store/stub tests/cfg_store/host_test.c \
 *      firmware/src/cfg_store.c -o /tmp/cfg && /tmp/cfg
 *
 * cfg_store.c is compiled UNCHANGED against a RAM "flash" (stub/), so the
 * test can plant the exact bytes 1.3.4 writes and read back the exact bytes
 * this firmware writes. Two promises are at stake, and before this file the
 * only way to check either was an OTA on a desk unit:
 *
 *   1. An update from 1.3.4 keeps every setting -- this record holds the
 *      brightness, the edition latch and, on WiFi builds, the network and
 *      the token -- and the new fields arrive as their defaults: Dark, and
 *      Focus off.
 *   2. A downgrade TO 1.3.4 still finds its settings. 1.3.4 knows nothing of
 *      the new fields; it reads its own 475-byte record off the front and
 *      checks its own CRC. If that CRC is not valid on what we wrote, 1.3.4
 *      sees a blank record and the unit forgets its edition latch.
 *
 * struct rec_134 below is a frozen copy of the 1.3.4 layout (63f2386,
 * firmware/src/cfg_store.c). It is a copy on purpose and must never be
 * "kept in step": it describes firmware already on desks.
 */
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/crc.h>
#include <zephyr/storage/flash_map.h>

#include "cfg_store.h"

uint8_t fake_flash[FAKE_FLASH_SIZE];
long fake_write_budget = -1;
int fake_writes;

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

#define REC_MAGIC 0xC1A46EF6u
#define SLOT_A 0
#define SLOT_B 4096

struct rec_134 {
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
	uint8_t edition_locked;
	uint32_t crc;
} __packed;

/* What 1.3.4's persist() does: erase the sector, write the record padded with
 * zeros to the 32-byte block. */
static void write_134(int slot, const struct rec_134 *r)
{
	uint8_t buf[ROUND_UP(sizeof(struct rec_134), 32)];
	struct rec_134 c = *r;

	c.magic = REC_MAGIC;
	c.crc = crc32_ieee((const uint8_t *)&c, offsetof(struct rec_134, crc));
	memset(buf, 0, sizeof(buf));
	memcpy(buf, &c, sizeof(c));
	memset(fake_flash + slot, 0xFF, 4096);
	memcpy(fake_flash + slot, buf, sizeof(buf));
}

/* What 1.3.4's slot_load() accepts as a current record. */
static bool read_134(int slot, struct rec_134 *out)
{
	memcpy(out, fake_flash + slot, sizeof(*out));
	return out->magic == REC_MAGIC &&
	       out->crc == crc32_ieee((const uint8_t *)out,
				      offsetof(struct rec_134, crc));
}

/* 1.3.4's cfg_init(): the valid slot with the higher sequence. */
static bool boot_134(struct rec_134 *out)
{
	struct rec_134 a, b;
	bool va = read_134(SLOT_A, &a), vb = read_134(SLOT_B, &b);

	if (va && (!vb || a.seq >= b.seq)) {
		*out = a;
		return true;
	}
	if (vb) {
		*out = b;
		return true;
	}
	return false;
}

/*
 * Write raw Focus bytes into every sealed record on the fake flash, and seal
 * it again -- what a newer firmware, or a flipped bit, would leave behind.
 * The offsets are the contract's, not cfg_store.c's: the 1.3.4 record, then
 * 26 theme bytes, then focus_on and focus_win, then the CRC over all of it.
 */
#define FOCUS_OFF (sizeof(struct rec_134) + 26)
#define FULL_CRC_OFF (FOCUS_OFF + 2)

static void plant_focus(uint8_t on, uint8_t win)
{
	const int slots[] = { SLOT_A, SLOT_B };

	for (int i = 0; i < 2; i++) {
		uint8_t *r = fake_flash + slots[i];
		uint32_t magic, crc;

		memcpy(&magic, r, sizeof(magic));
		memcpy(&crc, r + FULL_CRC_OFF, sizeof(crc));
		if (magic != REC_MAGIC || crc != crc32_ieee(r, FULL_CRC_OFF)) {
			continue;
		}
		r[FOCUS_OFF] = on;
		r[FOCUS_OFF + 1] = win;
		crc = crc32_ieee(r, FULL_CRC_OFF);
		memcpy(r + FULL_CRC_OFF, &crc, sizeof(crc));
	}
}

static struct rec_134 desk_record(void)
{
	struct rec_134 r;

	memset(&r, 0, sizeof(r));
	r.seq = 41;
	r.mode = CFG_MODE_USB;
	r.weekly_sel = 1;
	r.tz_set = 1;
	r.bright_pct = 60;
	r.tz_min = -300;
	strcpy(r.ssid, "desk-net");
	strcpy(r.psk, "hunter22");
	strcpy(r.ap_psk, "apkey12345");
	r.ota_state = 0;
	r.main_src = CFG_MAIN_SRC_CODEX;
	r.edition = CFG_EDITION_CODEX;
	r.edition_locked = 1;
	return r;
}

static void check_desk_settings(const char *when)
{
	char m[128], ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX], ap[CFG_AP_PSK_MAX];
	int32_t tz = 0;

	snprintf(m, sizeof(m), "%s: brightness, window and source kept", when);
	CHECK(cfg_get_bright_pct() == 60 && cfg_get_weekly_sel() == 1 &&
	      cfg_get_main_src() == CFG_MAIN_SRC_CODEX &&
	      cfg_get_mode() == CFG_MODE_USB, m);
	snprintf(m, sizeof(m), "%s: edition and its latch kept", when);
	CHECK(cfg_get_edition() == CFG_EDITION_CODEX && cfg_edition_locked(), m);
	snprintf(m, sizeof(m), "%s: timezone kept", when);
	CHECK(cfg_get_tz(&tz) && tz == -300, m);
	snprintf(m, sizeof(m), "%s: network and AP password kept", when);
	CHECK(cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk)) &&
	      strcmp(ssid, "desk-net") == 0 && strcmp(psk, "hunter22") == 0 &&
	      cfg_get_ap_psk(ap, sizeof(ap)) && strcmp(ap, "apkey12345") == 0,
	      m);
}

int main(void)
{
	char msg[160];
	struct cfg_theme th;
	struct rec_134 old;

	/* --- 1. A unit on 1.3.4 takes the update --- */
	memset(fake_flash, 0xFF, sizeof(fake_flash));
	old = desk_record();
	write_134(SLOT_A, &old);

	CHECK(cfg_init() == 0, "update: the store mounts");
	check_desk_settings("update");
	cfg_get_theme(&th);
	CHECK(th.id == 0, "update: the theme defaults to Dark");
	CHECK(cfg_get_focus_on() == 0 && cfg_get_focus_win() == 0,
	      "update: Focus defaults to off, on the session window");

	/* --- 2. A theme is chosen, and survives a reboot --- */
	memset(&th, 0, sizeof(th));
	th.id = 12;
	for (int i = 0; i < 21; i++) {
		th.rgb[i] = (uint8_t)(0x10 + i);
	}
	th.w = 22;
	th.ends = 1;
	th.dot = 0;
	th.by = 1;
	CHECK(cfg_set_theme(&th) == 0, "theme: stored");
	CHECK(cfg_set_focus(1, 1) == 0, "focus: stored");

	struct cfg_theme chosen = th;	/* what the owner picked, for later */

	{
		int before = fake_writes;

		CHECK(cfg_set_theme(&th) == 0 && fake_writes == before,
		      "theme: storing the same bytes again writes nothing");
	}

	CHECK(cfg_init() == 0, "reboot: the store mounts");
	{
		struct cfg_theme back;

		cfg_get_theme(&back);
		CHECK(memcmp(&back, &th, sizeof(th)) == 0,
		      "reboot: the custom theme comes back byte for byte");
	}
	CHECK(cfg_get_focus_on() == 1 && cfg_get_focus_win() == 1,
	      "reboot: Focus comes back");
	check_desk_settings("reboot");

	/* --- 3. The owner flashes 1.3.4 back --- */
	{
		struct rec_134 seen;

		CHECK(boot_134(&seen),
		      "downgrade: 1.3.4 finds a record it accepts");
		CHECK(seen.seq > old.seq,
		      "downgrade: ...and it is the newest one, not the old copy");
		CHECK(seen.bright_pct == 60 && seen.weekly_sel == 1 &&
		      seen.main_src == CFG_MAIN_SRC_CODEX &&
		      seen.tz_set == 1 && seen.tz_min == -300 &&
		      strcmp(seen.ssid, "desk-net") == 0,
		      "downgrade: 1.3.4 reads every one of its settings");
		CHECK(seen.edition == CFG_EDITION_CODEX &&
		      seen.edition_locked == 1,
		      "downgrade: the edition latch survives it");

		/*
		 * 1.3.4 then writes a record of its own (say, a brightness
		 * change). Its persist() goes to the slot it did NOT boot
		 * from, which is B: the last write above, the Focus one, went
		 * to A. So A still holds this firmware's whole record.
		 */
		seen.seq++;
		seen.bright_pct = 80;
		write_134(SLOT_B, &seen);
	}

	/* --- 4. And updates again --- */
	CHECK(cfg_init() == 0, "re-update: the store mounts");
	CHECK(cfg_get_bright_pct() == 80,
	      "re-update: the newer 1.3.4 record wins over ours");
	cfg_get_theme(&th);
	CHECK(memcmp(&th, &chosen, sizeof(th)) == 0 &&
	      cfg_get_focus_on() == 1 && cfg_get_focus_win() == 1,
	      "re-update: the theme and Focus come back from the slot 1.3.4"
	      " left alone");
	CHECK(cfg_get_edition() == CFG_EDITION_CODEX && cfg_edition_locked(),
	      "re-update: the edition latch is still there");

	/* Once 1.3.4 has written BOTH slots there is nothing left to take
	 * them from, and they are the defaults. */
	{
		struct rec_134 seen;

		CHECK(boot_134(&seen), "downgrade again: 1.3.4 boots");
		seen.seq += 2;
		write_134(SLOT_A, &seen);
		write_134(SLOT_B, &seen);
		seen.seq++;
		write_134(SLOT_A, &seen);
	}
	CHECK(cfg_init() == 0, "re-update 2: the store mounts");
	cfg_get_theme(&th);
	CHECK(th.id == 0 && cfg_get_focus_on() == 0 &&
	      cfg_get_bright_pct() == 80,
	      "re-update 2: with both slots 1.3.4's, theme and Focus are"
	      " defaults and its settings stand");

	/*
	 * A 1.3.4 record with junk after it. On an encrypted chip the bytes
	 * past what 1.3.4 wrote read back as decryption noise, not 0xFF and
	 * not zero; the theme bytes must come from nowhere but defaults.
	 */
	{
		struct rec_134 r = desk_record();

		memset(fake_flash, 0xFF, sizeof(fake_flash));
		write_134(SLOT_A, &r);
		for (int i = 480; i < 512; i++) {
			fake_flash[SLOT_A + i] = (uint8_t)(0xA5 ^ (i * 37));
		}
		CHECK(cfg_init() == 0, "garbage tail: the store mounts");
		check_desk_settings("garbage tail");
		cfg_get_theme(&th);
		CHECK(th.id == 0 && th.w == 0 && th.rgb[0] == 0 &&
		      cfg_get_focus_on() == 0,
		      "garbage tail: theme and Focus are defaults, not junk");
	}

	/*
	 * A power cut at every byte of a theme write.
	 *
	 * The window that matters is the last 32 bytes: past 1.3.4's own seal
	 * (byte 475) the new slot is a valid 1.3.4 record with a newer
	 * sequence, so it wins, and on its own it says "Dark". The theme the
	 * owner had is still sealed in the other slot, and has to come from
	 * there. Every cut must leave either the old theme or the new one --
	 * never Dark, never a mix, never the settings lost.
	 */
	{
		struct cfg_theme t1, t2, got;
		int bad = 0, old_seen = 0, new_seen = 0;

		memset(&t1, 0, sizeof(t1));
		t1.id = 4;			/* Ocean */
		t2 = chosen;			/* the custom one */
		for (long cut = 0; cut <= 512; cut++) {
			struct rec_134 r = desk_record();

			memset(fake_flash, 0xFF, sizeof(fake_flash));
			write_134(SLOT_A, &r);
			cfg_init();
			cfg_set_theme(&t1);	/* whole, to B */
			cfg_init();
			fake_write_budget = cut;
			cfg_set_theme(&t2);	/* to A, cut at `cut` bytes */
			fake_write_budget = -1;
			cfg_init();
			cfg_get_theme(&got);
			if (memcmp(&got, &t1, sizeof(got)) == 0) {
				old_seen++;
			} else if (memcmp(&got, &t2, sizeof(got)) == 0) {
				new_seen++;
			} else {
				bad++;
			}
			if (cfg_get_bright_pct() != 60 || !cfg_edition_locked()) {
				bad++;
			}
		}
		snprintf(msg, sizeof(msg), "power cut: all 513 cut points leave the"
			 " old theme or the new one (old %d, new %d, bad %d)",
			 old_seen, new_seen, bad);
		CHECK(bad == 0 && old_seen > 0 && new_seen > 0, msg);
	}

	/* --- 4b. From a build that already had the themes record ---
	 *
	 * fp/themes-fw stored the two Focus bytes but nothing ever set them,
	 * so every record it wrote carries zeros there. The unit takes this
	 * build: its theme and every 1.3.4 setting stay, and Focus is off.
	 * The record is written by THIS cfg_store (it is the same layout),
	 * with the Focus bytes forced back to what that build left in them.
	 */
	memset(fake_flash, 0xFF, sizeof(fake_flash));
	old = desk_record();
	write_134(SLOT_A, &old);
	CHECK(cfg_init() == 0, "themes build: the store mounts");
	memset(&th, 0, sizeof(th));
	th.id = 9;		/* Light */
	CHECK(cfg_set_theme(&th) == 0, "themes build: Light stored");
	CHECK(cfg_init() == 0, "themes build -> focus: the store mounts");
	cfg_get_theme(&th);
	CHECK(th.id == 9, "themes build -> focus: the theme is kept");
	check_desk_settings("themes build -> focus");
	CHECK(cfg_get_focus_on() == 0 && cfg_get_focus_win() == 0,
	      "themes build -> focus: Focus is off, on the session window");

	/* Each setting is its own: storing one never moves the other. */
	CHECK(cfg_set_focus(1, 1) == 0, "focus: on, weekly");
	cfg_get_theme(&th);
	CHECK(th.id == 9, "focus: storing Focus leaves the theme alone");
	th.id = 2;
	CHECK(cfg_set_theme(&th) == 0, "theme: Bold");
	CHECK(cfg_get_focus_on() == 1 && cfg_get_focus_win() == 1,
	      "theme: storing a theme leaves Focus alone");
	/* Off keeps the window, so turning it back on lands where it was. */
	CHECK(cfg_set_focus(0, 1) == 0 && cfg_get_focus_on() == 0 &&
	      cfg_get_focus_win() == 1, "focus: off remembers weekly");
	CHECK(cfg_init() == 0 && cfg_get_focus_on() == 0 &&
	      cfg_get_focus_win() == 1, "reboot: off, and weekly, both kept");

	/* A byte this build never writes reads as the default, not as "on":
	 * a future firmware's value after a downgrade, or a bit flip, must
	 * not leave the panel in a mode Settings cannot show. */
	CHECK(cfg_set_focus(1, 1) == 0, "focus: on, weekly again");
	CHECK(cfg_set_focus(7, 0) != 0 && cfg_set_focus(1, 9) != 0,
	      "focus: out-of-range values are refused");
	CHECK(cfg_get_focus_on() == 1 && cfg_get_focus_win() == 1,
	      "focus: ...and a refusal leaves the stored choice alone");
	plant_focus(7, 9);
	CHECK(cfg_init() == 0 && cfg_get_focus_on() == 0 &&
	      cfg_get_focus_win() == 0,
	      "focus: bytes this build never writes read as off, session");
	check_desk_settings("focus: odd bytes");
	{
		struct rec_134 seen;

		CHECK(boot_134(&seen) && seen.bright_pct == 60 &&
		      seen.edition_locked == 1,
		      "focus: 1.3.4 still reads the record Focus wrote");
	}

	/* --- 5. A blank partition --- */
	memset(fake_flash, 0xFF, sizeof(fake_flash));
	CHECK(cfg_init() == 0, "blank: the store mounts");
	cfg_get_theme(&th);
	CHECK(th.id == 0 && th.w == 0 && cfg_get_focus_on() == 0 &&
	      cfg_get_bright_pct() == 100, "blank: everything is its default");

	/* --- 6. A factory reset keeps the latch and drops the theme --- */
	cfg_set_edition(CFG_EDITION_CLAUDE);
	th.id = 5;
	cfg_set_theme(&th);
	cfg_reset();
	cfg_get_theme(&th);
	CHECK(th.id == 0 && cfg_edition_locked(),
	      "reset: theme back to Dark, edition latch kept");
	{
		struct rec_134 seen;

		CHECK(boot_134(&seen) && seen.edition_locked == 1,
		      "reset: the record 1.3.4 would read is still latched");
	}

	printf(failures ? "\n%d FAILED\n" : "\nall cfg_store checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
