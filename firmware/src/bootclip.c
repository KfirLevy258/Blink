/*
 * Pick the boot clip for this unit. See bootclip.h for why this exists.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include "bootclip.h"
#include "bootanim.h"
#include "bootanim_codex.h"
#include "sleepanim_claude_close.h"
#include "sleepanim_claude_loop.h"
#include "sleepanim_claude_open.h"
#include "sleepanim_codex_close.h"
#include "sleepanim_codex_loop.h"
#include "sleepanim_codex_open.h"
#include "cfg_store.h"

/*
 * No `last` frame here on purpose.
 *
 * The struct used to carry a pointer to each clip's one-frame blob for a skip
 * path that was removed in 2026-07 (flashing the final frame during setup
 * reboots read as an unexplained face pop). Nothing ever read the fields --
 * but TAKING THEIR ADDRESS from a static table is a reference, so the linker
 * could no longer drop the blobs, and 6,327 bytes of flash were kept alive for
 * two members with no readers. ui_boot.c still says the linker drops them
 * while unreferenced, and with these gone that is true again.
 */
static const struct bootclip clips[] = {
	[CFG_EDITION_CLAUDE] = {
		.blob = bootanim_blob,
		.blob_len = sizeof(bootanim_blob),
		.bg_rgb = BOOTANIM_BG_RGB,
		.name = "claude",
	},
	[CFG_EDITION_CODEX] = {
		.blob = codex_bootanim_blob,
		.blob_len = sizeof(codex_bootanim_blob),
		.bg_rgb = CODEX_BOOTANIM_BG_RGB,
		.name = "codex",
	},
};

const struct bootclip *bootclip_active(void)
{
	static const struct bootclip *chosen;

	if (chosen != NULL) {
		return chosen;
	}

	uint8_t e = cfg_get_edition();

	/*
	 * Out of range falls back to Claude rather than refusing to boot. A
	 * value this code does not recognise can only come from a record
	 * written by a LATER firmware -- a downgrade -- and the honest
	 * behaviour there is the edition that has always shipped, not a blank
	 * screen. cfg_set_edition validates on the way in; this is the
	 * belt-and-braces on the way out.
	 */
	if (e >= ARRAY_SIZE(clips)) {
		printk("[boot] unknown edition %u; playing the Claude clip\n", e);
		e = CFG_EDITION_CLAUDE;
	}
	chosen = &clips[e];
	printk("[boot] edition: %s\n", chosen->name);
	return chosen;
}

/* Sleep clips (docs/sleep-mode-design.md), one set per edition. Drawn by
 * tools/make_sleepanim.py, encoded by tools/encode_bootanim.py --frames;
 * identical shapes in each edition's own ground and ink. */
#define SLEEP_SET(ed, ED)							\
	{								\
		[SLEEP_CLOSE] = { ed##_sleep_close_bootanim_blob,	\
				  sizeof(ed##_sleep_close_bootanim_blob), \
				  ED##_SLEEP_CLOSE_BOOTANIM_BG_RGB, #ed },	\
		[SLEEP_LOOP] = { ed##_sleep_loop_bootanim_blob,		\
				 sizeof(ed##_sleep_loop_bootanim_blob),	\
				 ED##_SLEEP_LOOP_BOOTANIM_BG_RGB, #ed },	\
		[SLEEP_OPEN] = { ed##_sleep_open_bootanim_blob,		\
				 sizeof(ed##_sleep_open_bootanim_blob),	\
				 ED##_SLEEP_OPEN_BOOTANIM_BG_RGB, #ed },	\
	}

static const struct bootclip sleep_clips[][3] = {
	[CFG_EDITION_CLAUDE] = SLEEP_SET(claude, CLAUDE),
	[CFG_EDITION_CODEX] = SLEEP_SET(codex, CODEX),
};

const struct bootclip *sleepclip_active(enum sleep_part part)
{
	uint8_t e = cfg_get_edition();

	if (e >= ARRAY_SIZE(sleep_clips)) {
		e = CFG_EDITION_CLAUDE;	/* same fallback as the boot clip */
	}
	if (part > SLEEP_OPEN) {
		part = SLEEP_LOOP;
	}
	return &sleep_clips[e][part];
}
