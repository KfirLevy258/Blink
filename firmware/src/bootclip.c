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

static const struct bootclip clips[] = {
	[CFG_EDITION_CLAUDE] = {
		.blob = bootanim_blob,
		.blob_len = sizeof(bootanim_blob),
		.last = bootanim_last,
		.last_len = sizeof(bootanim_last),
		.bg_rgb = BOOTANIM_BG_RGB,
		.name = "claude",
	},
	[CFG_EDITION_CODEX] = {
		.blob = codex_bootanim_blob,
		.blob_len = sizeof(codex_bootanim_blob),
		.last = codex_bootanim_last,
		.last_len = sizeof(codex_bootanim_last),
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
