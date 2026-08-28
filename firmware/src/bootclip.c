/*
 * Pick the boot clip for this unit. See bootclip.h for why this exists.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include "bootclip.h"
#include "bootanim.h"
#include "bootanim_codex.h"
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
