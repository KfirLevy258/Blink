/* The config record across an update and a downgrade, on a laptop.
 *
 * Build & run (tests/ci/check_host_tests.sh does this):
 *   cc -DCFG_STUB_CAPTURE -I firmware/src -I tests/cfg_store/stub \
 *      tests/cfg_store/host_test.c \
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
/* memmem(), which glibc hides without it; macOS has it regardless. */
#define _GNU_SOURCE
#include <errno.h>
#include <stdarg.h>
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
int fake_erase_rc;
int fake_erase_pass;

static int failures;
static char msg_buf[256];
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

/* Everything the store printed since the last reset, for the boot-line
 * checks. Truncates rather than overruns; 4 KiB is far more than one
 * cfg_init() prints. */
static char logged[4096];
static size_t logged_len;

int stub_printk(const char *fmt, ...)
{
	char line[512];
	va_list ap;

	va_start(ap, fmt);
	int n = vsnprintf(line, sizeof(line), fmt, ap);

	va_end(ap);
	printf("  log: %s", line);
	size_t room = sizeof(logged) - 1 - logged_len;
	size_t len = strlen(line);

	if (len > room) {
		len = room;
	}
	memcpy(logged + logged_len, line, len);
	logged_len += len;
	logged[logged_len] = '\0';
	return n;
}

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
 * The offsets are the contract's, not cfg_store.c's: the 1.3.4 record, then
 * 26 theme bytes, then focus_on and focus_win, then 1.4.x's CRC over all of
 * that (crc_143), then pair_set, the 32-byte pairing key, and the CRC over
 * everything (spec section 5.2).
 */
#define FOCUS_OFF (sizeof(struct rec_134) + 26)
#define CRC_143_OFF (FOCUS_OFF + 2)
#define PAIR_SET_OFF (CRC_143_OFF + 4)
#define PAIR_KEY_OFF (PAIR_SET_OFF + 1)
#define WIFI_CRC_OFF (PAIR_KEY_OFF + 32)
#define WIFI_WIRE ROUND_UP(WIFI_CRC_OFF + 4, 32)

static bool sealed_at(int slot, size_t crc_off)
{
	const uint8_t *r = fake_flash + slot;
	uint32_t magic, crc;

	memcpy(&magic, r, sizeof(magic));
	memcpy(&crc, r + crc_off, sizeof(crc));
	return magic == REC_MAGIC && crc == crc32_ieee(r, crc_off);
}

/* Is this slot a whole record of the WiFi layout, outer seal and all? */
static bool slot_whole_wifi(int slot)
{
	return sealed_at(slot, WIFI_CRC_OFF);
}

/*
 * Write raw Focus bytes into every sealed record on the fake flash, and seal
 * it again -- what a newer firmware, or a flipped bit, would leave behind.
 * Both seals that cover them, so a whole record stays whole.
 */
static void plant_focus(uint8_t on, uint8_t win)
{
	const int slots[] = { SLOT_A, SLOT_B };

	for (int i = 0; i < 2; i++) {
		uint8_t *r = fake_flash + slots[i];
		bool whole = slot_whole_wifi(slots[i]);
		uint32_t crc;

		if (!sealed_at(slots[i], CRC_143_OFF)) {
			continue;
		}
		r[FOCUS_OFF] = on;
		r[FOCUS_OFF + 1] = win;
		crc = crc32_ieee(r, CRC_143_OFF);
		memcpy(r + CRC_143_OFF, &crc, sizeof(crc));
		if (whole) {
			crc = crc32_ieee(r, WIFI_CRC_OFF);
			memcpy(r + WIFI_CRC_OFF, &crc, sizeof(crc));
		}
	}
}

/*
 * A frozen copy of the 1.4.x record (the layout this file tested before the
 * pairing tail): 1.3.4's record, its CRC being crc_134, then the theme and
 * Focus bytes and the CRC over all of it. Like rec_134 it describes firmware
 * already on desks and must never be "kept in step".
 */
struct rec_143 {
	struct rec_134 r;
	uint8_t theme[26];	/* struct cfg_theme's bytes, in order */
	uint8_t focus_on;
	uint8_t focus_win;
	uint32_t crc;
} __packed;

/* What 1.4.x's persist() does: both seals, padded with zeros to 512. */
static void write_143(int slot, const struct rec_143 *x)
{
	uint8_t buf[ROUND_UP(sizeof(struct rec_143), 32)];
	struct rec_143 c = *x;

	c.r.magic = REC_MAGIC;
	c.r.crc = crc32_ieee((const uint8_t *)&c.r,
			     offsetof(struct rec_134, crc));
	c.crc = crc32_ieee((const uint8_t *)&c, offsetof(struct rec_143, crc));
	memset(buf, 0, sizeof(buf));
	memcpy(buf, &c, sizeof(c));
	memset(fake_flash + slot, 0xFF, 4096);
	memcpy(fake_flash + slot, buf, sizeof(buf));
}

/* 1.4.x's cfg_init() on whole records of its own layout: the valid slot with
 * the higher sequence. */
static bool boot_143(struct rec_143 *out)
{
	struct rec_143 a, b;
	bool va = sealed_at(SLOT_A, offsetof(struct rec_143, crc));
	bool vb = sealed_at(SLOT_B, offsetof(struct rec_143, crc));

	memcpy(&a, fake_flash + SLOT_A, sizeof(a));
	memcpy(&b, fake_flash + SLOT_B, sizeof(b));
	if (va && (!vb || a.r.seq >= b.r.seq)) {
		*out = a;
		return true;
	}
	if (vb) {
		*out = b;
		return true;
	}
	return false;
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

/* --- The WiFi pairing (spec section 5.2, D28, D33, D44) --- */

/* A network name that is not UTF-8 (a bare 0xff, then a snowman), and a
 * password with a quote and a backslash in it: Review Focus 4. */
static const uint8_t P1_SSID[] = { 0xff, 'a', 0xe2, 0x98, 0x83 };
static const char P1_PSK[] = "p\"q\\r";
static uint8_t P1_KEY[CFG_PAIR_KEY_LEN];	/* 0..31, filled in main */
static const uint8_t P2_SSID[] = { 'o', 't', 'h', 'e', 'r', '-', 'n', 'e', 't' };
static const char P2_PSK[] = "second-pass-word";
static uint8_t P2_KEY[CFG_PAIR_KEY_LEN];	/* 0x80.., filled in main */

static int set_p1(void)
{
	return cfg_set_pairing(P1_SSID, sizeof(P1_SSID), P1_PSK, P1_KEY);
}

static int set_p2(void)
{
	return cfg_set_pairing(P2_SSID, sizeof(P2_SSID), P2_PSK, P2_KEY);
}

/* Does the store hand back exactly this pairing, every part of it? */
static bool pairing_is(const uint8_t *ssid, size_t n, const char *psk,
		       const uint8_t *key)
{
	uint8_t s[32], k[CFG_PAIR_KEY_LEN];
	char p[CFG_PSK_MAX];
	size_t len = 0;

	if (!cfg_get_pairing(s, &len, p, sizeof(p), k)) {
		return false;
	}
	return len == n && memcmp(s, ssid, n) == 0 && strcmp(p, psk) == 0 &&
	       memcmp(k, key, sizeof(k)) == 0;
}

/* Any byte run of a secret anywhere on the flash, both sectors. */
static bool on_flash(const void *p, size_t n)
{
	return memmem(fake_flash, sizeof(fake_flash), p, n) != NULL;
}

static bool p1_on_flash(void)
{
	return on_flash(P1_SSID, sizeof(P1_SSID)) ||
	       on_flash(P1_PSK, strlen(P1_PSK)) ||
	       on_flash(P1_KEY, sizeof(P1_KEY));
}

/* A desk on 1.3.4, updated: the store mounted on its record in `slot`. */
static void desk_in(int slot)
{
	struct rec_134 r = desk_record();

	memset(fake_flash, 0xFF, sizeof(fake_flash));
	write_134(slot, &r);
	cfg_init();
}

static void named(const char *name, int before)
{
	printf(failures == before ? "PASS %s\n" : "FAIL %s\n", name);
}

static void pairing_round_trip(void)
{
	int f = failures, before;
	uint8_t nul[] = { 'a', 0x00, 'b' }, long_ssid[33];
	char long_psk[66];

	desk_in(SLOT_A);
	CHECK(!cfg_paired(), "round trip: not paired to start");
	CHECK(set_p1() == 0, "round trip: stored");
	CHECK(cfg_paired() && pairing_is(P1_SSID, sizeof(P1_SSID), P1_PSK, P1_KEY),
	      "round trip: read back byte for byte");
	CHECK(cfg_init() == 0 && cfg_paired() &&
	      pairing_is(P1_SSID, sizeof(P1_SSID), P1_PSK, P1_KEY),
	      "round trip: survives a reboot");
	CHECK(cfg_get_bright_pct() == 60 && cfg_edition_locked(),
	      "round trip: the other settings kept");

	memset(long_ssid, 'x', sizeof(long_ssid));
	memset(long_psk, 'y', sizeof(long_psk) - 1);
	long_psk[sizeof(long_psk) - 1] = '\0';	/* 65 characters */
	before = fake_writes;
	CHECK(cfg_set_pairing(nul, sizeof(nul), "pw", P2_KEY) == -EINVAL &&
	      cfg_set_pairing(long_ssid, 33, "pw", P2_KEY) == -EINVAL &&
	      cfg_set_pairing(long_ssid, 0, "pw", P2_KEY) == -EINVAL &&
	      cfg_set_pairing(long_ssid, 32, long_psk, P2_KEY) == -EINVAL &&
	      fake_writes == before,
	      "round trip: a 0x00 byte, 0 or 33 bytes of SSID, a 65-char"
	      " password are refused, nothing written");
	CHECK(cfg_set_pairing(long_ssid, 32, long_psk + 1, P2_KEY) == 0 &&
	      pairing_is(long_ssid, 32, long_psk + 1, P2_KEY),
	      "round trip: 32 bytes of SSID and 64 of password fit");
	CHECK(pairing_is(long_ssid, 32, long_psk + 1, P2_KEY) &&
	      !cfg_get_pairing(long_ssid, &(size_t){ 0 }, long_psk, 64, P2_KEY),
	      "round trip: a password buffer too small is refused, not cut");
	named("pairing_round_trip", f);
}

static void old_wifi_build_creds_not_paired(void)
{
	int f = failures;
	uint8_t s[32], k[CFG_PAIR_KEY_LEN];
	char p[CFG_PSK_MAX];
	size_t n;

	/* desk_record() holds "desk-net" and "hunter22", as a board that ran
	 * the deleted WiFi build does, and no pair_set: there was none. */
	desk_in(SLOT_A);
	CHECK(!cfg_paired() && !cfg_get_pairing(s, &n, p, sizeof(p), k),
	      "old WiFi build: its SSID and password do not make it paired");
	check_desk_settings("old WiFi build");
	named("old_wifi_build_creds_not_paired", f);
}

static void clear_pairing_wipes_both_sectors(void)
{
	int f = failures;

	desk_in(SLOT_A);
	CHECK(set_p1() == 0 && cfg_set_focus(1, 0) == 0,
	      "forget: paired, and written again so both sectors hold it");
	CHECK(slot_whole_wifi(SLOT_A) && slot_whole_wifi(SLOT_B) &&
	      on_flash(P1_KEY, sizeof(P1_KEY)),
	      "forget: (the key is on flash to start with)");
	CHECK(cfg_clear_pairing() == 0, "forget: succeeds");
	CHECK(!on_flash(P1_SSID, sizeof(P1_SSID)) &&
	      !on_flash(P1_PSK, strlen(P1_PSK)) &&
	      !on_flash(P1_KEY, sizeof(P1_KEY)),
	      "forget: no byte run of the SSID, password or key in either"
	      " sector");
	CHECK(slot_whole_wifi(SLOT_A) && slot_whole_wifi(SLOT_B),
	      "forget: both sectors hold a whole record");
	CHECK(!cfg_paired() && cfg_init() == 0 && !cfg_paired(),
	      "forget: unpaired, and still after a reboot");
	CHECK(cfg_get_edition() == CFG_EDITION_CODEX && cfg_edition_locked() &&
	      cfg_get_bright_pct() == 60 && cfg_get_focus_on() == 1,
	      "forget: the edition and every other setting kept");
	named("clear_pairing_wipes_both_sectors", f);
}

static void clear_pairing_reports_erase_failure(void)
{
	int f = failures;

	desk_in(SLOT_A);
	set_p1();
	cfg_set_focus(1, 0);
	fake_erase_pass = 1;	/* the first sector clears, the second will not */
	fake_erase_rc = -EIO;
	CHECK(cfg_clear_pairing() != 0,
	      "forget, second erase fails: reported as a failure");
	fake_erase_rc = 0;
	fake_erase_pass = 0;
	CHECK(!cfg_paired(), "forget, second erase fails: unpaired in RAM");
	CHECK(cfg_init() == 0 && !cfg_paired(),
	      "forget, second erase fails: the cleared record wins the reboot");
	CHECK(cfg_clear_pairing() == 0 && !p1_on_flash(),
	      "forget, second erase fails: a retry clears both sectors");
	named("clear_pairing_reports_erase_failure", f);
}

static struct rec_143 desk_143(uint8_t theme_id)
{
	struct rec_143 x;

	memset(&x, 0, sizeof(x));
	x.r = desk_record();
	x.theme[0] = theme_id;
	x.theme[22] = 12;	/* w: a custom theme is stored */
	x.focus_on = 1;
	x.focus_win = 1;
	return x;
}

static void theme_survives_upgrade_from_143(void)
{
	int f = failures;
	struct rec_143 x = desk_143(7);
	struct cfg_theme th;

	memset(fake_flash, 0xFF, sizeof(fake_flash));
	write_143(SLOT_A, &x);
	logged_len = 0;
	logged[0] = '\0';
	CHECK(cfg_init() == 0, "from 1.4.x: the store mounts");
	cfg_get_theme(&th);
	CHECK(th.id == 7 && th.w == 12 && cfg_get_focus_on() == 1 &&
	      cfg_get_focus_win() == 1, "from 1.4.x: theme and Focus kept");
	check_desk_settings("from 1.4.x");
	CHECK(!cfg_paired(), "from 1.4.x: not paired");
	CHECK(strstr(logged, "taken from the older slot") == NULL,
	      "from 1.4.x: nothing taken from the other slot");
	named("theme_survives_upgrade_from_143", f);
}

static void theme_survives_downgrade_to_143(void)
{
	int f = failures;
	struct rec_143 seen;
	struct cfg_theme th;

	desk_in(SLOT_A);
	memset(&th, 0, sizeof(th));
	th.id = 5;
	CHECK(cfg_set_theme(&th) == 0 && set_p1() == 0,
	      "to 1.4.x: a theme and a pairing stored");
	CHECK(boot_143(&seen), "to 1.4.x: 1.4.x finds a whole record");
	CHECK(seen.theme[0] == 5 && seen.r.bright_pct == 60 &&
	      seen.r.edition == CFG_EDITION_CODEX && seen.r.edition_locked == 1,
	      "to 1.4.x: it reads the theme, the settings and the latch");
	CHECK(slot_whole_wifi(SLOT_A) || slot_whole_wifi(SLOT_B),
	      "to 1.4.x: (the record is ours, with the pairing tail)");
	named("theme_survives_downgrade_to_143", f);
}

static void downgrade_write_then_upgrade(void)
{
	int f = failures;
	struct rec_143 seen;
	struct cfg_theme th;

	/* A: ours, theme 5, paired -- the last write. */
	memset(fake_flash, 0xFF, sizeof(fake_flash));
	cfg_init();
	memset(&th, 0, sizeof(th));
	th.id = 5;
	cfg_set_theme(&th);	/* A */
	set_p1();		/* B */
	cfg_set_focus(1, 0);	/* A */
	CHECK(slot_whole_wifi(SLOT_A) && cfg_paired(),
	      "downgrade write: A is ours and paired");
	/* 1.4.x boots it and writes theme 2 to B, with a newer sequence. */
	CHECK(boot_143(&seen), "downgrade write: 1.4.x boots");
	seen.r.seq++;
	seen.theme[0] = 2;
	write_143(SLOT_B, &seen);

	logged_len = 0;
	logged[0] = '\0';
	CHECK(cfg_init() == 0, "downgrade write, upgrade: the store mounts");
	cfg_get_theme(&th);
	CHECK(th.id == 2, "downgrade write, upgrade: 1.4.x's newer theme wins");
	CHECK(!cfg_paired(),
	      "downgrade write, upgrade: the pairing 1.4.x could not see is"
	      " not brought back");
	CHECK(strstr(logged, "taken from the older slot") == NULL,
	      "downgrade write, upgrade: nothing taken from the older slot");
	named("downgrade_write_then_upgrade_keeps_newer_theme_and_drops_pairing",
	      f);
}

static void torn_tail_never_pairs_with_old_key(void)
{
	int f = failures;
	struct cfg_theme th;
	char ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];

	memset(fake_flash, 0xFF, sizeof(fake_flash));
	cfg_init();
	memset(&th, 0, sizeof(th));
	th.id = 5;
	cfg_set_theme(&th);	/* A */
	set_p1();		/* B */
	cfg_set_focus(1, 0);	/* A: theme 5, P1 */
	/* A new pairing to B, the power gone right after 1.3.4's seal: the new
	 * SSID and password are sealed, the new key never reached flash. */
	fake_write_budget = (long)sizeof(struct rec_134);
	set_p2();
	fake_write_budget = -1;
	CHECK(fake_flash[SLOT_B + sizeof(struct rec_134)] == 0xFF &&
	      fake_flash[SLOT_B + WIFI_WIRE - 1] == 0xFF,
	      "torn tail: (B's tail is erased flash)");
	CHECK(cfg_init() == 0, "torn tail: the store mounts");
	CHECK(cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk)) &&
	      strcmp(ssid, "other-net") == 0,
	      "torn tail: B's newer prefix wins");
	CHECK(!cfg_paired(),
	      "torn tail: the new SSID is never paired with A's old key");
	cfg_get_theme(&th);
	CHECK(th.id == 5 && cfg_get_focus_on() == 1,
	      "torn tail: theme and Focus taken from A");
	named("torn_tail_never_pairs_with_old_key", f);
}

/*
 * A power cut at every byte of a pairing write, and at every byte of a
 * Forget across both of its writes, landing first in A and then in B. One
 * record always loads whole, the settings stand, and the board is paired
 * only with a pairing whose every byte reached flash.
 */
static void power_cut_every_byte_both_slots(void)
{
	int f = failures;
	const int slots[] = { SLOT_A, SLOT_B };

	for (int i = 0; i < 2; i++) {
		int bad = 0, p1 = 0, p2 = 0, none = 0;

		for (long cut = 0; cut <= (long)WIFI_WIRE; cut++) {
			desk_in(slots[i]);	/* lives in slots[i] */
			set_p1();		/* to the other */
			cfg_init();
			fake_write_budget = cut;
			set_p2();		/* to slots[i], cut */
			fake_write_budget = -1;
			cfg_init();
			if (!cfg_paired()) {
				none++;
			} else if (pairing_is(P1_SSID, sizeof(P1_SSID), P1_PSK,
					      P1_KEY)) {
				p1++;
			} else if (pairing_is(P2_SSID, sizeof(P2_SSID), P2_PSK,
					      P2_KEY) && cut >= (long)WIFI_WIRE) {
				p2++;
			} else {
				bad++;
			}
			if (cfg_get_bright_pct() != 60 || !cfg_edition_locked() ||
			    !(slot_whole_wifi(SLOT_A) || slot_whole_wifi(SLOT_B))) {
				bad++;
			}
		}
		snprintf(msg_buf, sizeof(msg_buf),
			 "power cut, pairing write to slot %c: every cut leaves the old"
			 " pairing, the whole new one or none (old %d, new %d,"
			 " none %d, bad %d)", i ? 'B' : 'A', p1, p2, none, bad);
		CHECK(bad == 0 && p1 > 0 && p2 > 0 && none > 0, msg_buf);
	}

	for (int i = 0; i < 2; i++) {
		int bad = 0, p1 = 0, none = 0, clean = 0;

		for (long cut = 0; cut <= 2 * (long)WIFI_WIRE; cut++) {
			desk_in(slots[i]);
			set_p1();
			cfg_set_focus(1, 0);	/* both sectors hold P1 */
			cfg_init();
			fake_write_budget = cut;
			cfg_clear_pairing();
			fake_write_budget = -1;
			cfg_init();
			if (!cfg_paired()) {
				none++;
			} else if (pairing_is(P1_SSID, sizeof(P1_SSID), P1_PSK,
					      P1_KEY)) {
				p1++;
			} else {
				bad++;
			}
			if (cut == 2 * (long)WIFI_WIRE) {
				clean = !p1_on_flash();
			}
			if (cfg_get_bright_pct() != 60 || !cfg_edition_locked() ||
			    cfg_get_focus_on() != 1) {
				bad++;
			}
		}
		snprintf(msg_buf, sizeof(msg_buf),
			 "power cut, forget from slot %c: every cut leaves the old"
			 " pairing or none, settings kept, a whole forget leaves"
			 " nothing (old %d, none %d, bad %d, clean %d)",
			 i ? 'B' : 'A', p1, none, bad, clean);
		CHECK(bad == 0 && p1 > 0 && none > 0 && clean, msg_buf);
	}
	named("power_cut_every_byte_both_slots", f);
}

static void factory_reset_clears_pairing(void)
{
	int f = failures;

	desk_in(SLOT_A);
	set_p1();
	cfg_set_focus(1, 0);
	CHECK(cfg_reset() == 0 && !cfg_paired(), "reset: unpaired");
	CHECK(!p1_on_flash(), "reset: no byte run of the pairing on flash");
	CHECK(cfg_init() == 0 && !cfg_paired() && cfg_edition_locked() &&
	      cfg_get_edition() == CFG_EDITION_CODEX,
	      "reset: still unpaired after a reboot, the latch kept");
	named("factory_reset_clears_pairing", f);
}

static void edition_latch_untouched_by_clear_pairing(void)
{
	int f = failures;
	struct rec_134 a, b;

	desk_in(SLOT_A);	/* stamped Codex, latched */
	set_p1();
	CHECK(cfg_clear_pairing() == 0 && cfg_get_edition() == CFG_EDITION_CODEX &&
	      cfg_edition_locked(), "forget: the edition and latch kept in RAM");
	CHECK(read_134(SLOT_A, &a) && read_134(SLOT_B, &b) &&
	      a.edition == CFG_EDITION_CODEX && a.edition_locked == 1 &&
	      b.edition == CFG_EDITION_CODEX && b.edition_locked == 1,
	      "forget: both sectors carry the latch, as even 1.3.4 reads them");
	CHECK(cfg_set_edition(CFG_EDITION_CLAUDE) == -EPERM,
	      "forget: a stamp is still refused");
	named("edition_latch_untouched_by_clear_pairing", f);
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

	logged_len = 0;
	logged[0] = '\0';
	CHECK(cfg_init() == 0, "update: the store mounts");
	check_desk_settings("update");
	/* The record still holds "desk-net" (check_desk_settings just read it
	 * back), and the daemon copies every boot line into bridge.log, the
	 * file customers send with a support request. */
	CHECK(strstr(logged, "[cfg] mode=") != NULL &&
	      strstr(logged, "ssid=present") != NULL &&
	      strstr(logged, "desk-net") == NULL,
	      "boot line: says a network is stored without naming it");
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
	{
		/* A tap on the Focus button already lit cost a sector erase
		 * and a power-cut window before this (final review N2). No
		 * write here also keeps step 3's slot arithmetic true: the
		 * Focus write above is still the last one, and went to A. */
		int before = fake_writes;

		CHECK(cfg_set_focus(1, 1) == 0 && fake_writes == before,
		      "focus: storing the same choice again writes nothing");
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
		for (long cut = 0; cut <= (long)WIFI_WIRE; cut++) {
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
		snprintf(msg, sizeof(msg), "power cut: all %d cut points leave the"
			 " old theme or the new one (old %d, new %d, bad %d)",
			 (int)WIFI_WIRE + 1, old_seen, new_seen, bad);
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

	/* --- A write that fails leaves RAM as it was ---
	 *
	 * Both setters put the new values into the RAM record before they
	 * write it, and until the review of 1cb4fd6 (M1) neither put the old
	 * ones back on a failure. Everything reads that RAM -- theme_now, the
	 * screen's "stored" theme, and each setter's own "nothing changed"
	 * check -- so after one failure the board reported an unstored theme
	 * as held, and a retry of the same values wrote nothing and returned
	 * success. A flash refusing erases is the simplest failure to plant;
	 * a failed write after a good erase takes the same path in persist().
	 */
	{
		struct cfg_theme was, back, want;
		int before;

		CHECK(cfg_set_focus(1, 1) == 0, "failed write: Focus on, to start");
		cfg_get_theme(&was);
		want = was;
		want.id = (uint8_t)(was.id == 4 ? 5 : 4);
		fake_erase_rc = -5;
		CHECK(cfg_set_theme(&want) != 0, "failed write: theme refused");
		cfg_get_theme(&back);
		CHECK(memcmp(&back, &was, sizeof(was)) == 0,
		      "failed write: the theme in RAM is the one on flash");
		CHECK(cfg_set_focus(0, 0) != 0, "failed write: Focus refused");
		CHECK(cfg_get_focus_on() == 1 && cfg_get_focus_win() == 1,
		      "failed write: Focus in RAM is the one on flash");
		fake_erase_rc = 0;

		before = fake_writes;
		CHECK(cfg_set_theme(&want) == 0 && fake_writes > before,
		      "failed write, retried: the theme is really written");
		before = fake_writes;
		CHECK(cfg_set_focus(0, 0) == 0 && fake_writes > before,
		      "failed write, retried: Focus is really written");
		CHECK(cfg_init() == 0, "failed write, retried: reboot");
		cfg_get_theme(&back);
		CHECK(back.id == want.id && cfg_get_focus_on() == 0 &&
		      cfg_get_focus_win() == 0,
		      "failed write, retried: both survive the reboot");
		check_desk_settings("failed write, retried");
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

	for (int i = 0; i < CFG_PAIR_KEY_LEN; i++) {
		P1_KEY[i] = (uint8_t)i;
		P2_KEY[i] = (uint8_t)(0x80 + i);
	}
	pairing_round_trip();
	old_wifi_build_creds_not_paired();
	clear_pairing_wipes_both_sectors();
	clear_pairing_reports_erase_failure();
	theme_survives_upgrade_from_143();
	theme_survives_downgrade_to_143();
	downgrade_write_then_upgrade();
	torn_tail_never_pairs_with_old_key();
	power_cut_every_byte_both_slots();
	factory_reset_clears_pairing();
	edition_latch_untouched_by_clear_pairing();

	printf(failures ? "\n%d FAILED\n" : "\nall cfg_store checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
