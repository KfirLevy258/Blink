#ifndef BOOTCLIP_H
#define BOOTCLIP_H

#include <stddef.h>
#include <stdint.h>

/*
 * Which boot clip this unit plays, chosen at runtime from the stored edition.
 *
 * Two consumers need the same answer -- ui_boot plays it at power-on and
 * ui_anim replays it on the right-swipe -- and before this they both reached
 * straight into bootanim.h for `bootanim_blob`. With a second clip compiled in
 * that stops working: the symbols collide, and worse, the two could disagree
 * about which edition they think they are. One accessor, one answer.
 *
 * The clip is a FACTORY fact (see cfg_edition in cfg_store.h), so it is read
 * once and cached. Provisioning a unit therefore takes effect on the next
 * boot rather than immediately, which is correct in the only way that
 * matters: the thing being configured is a boot animation.
 */
struct bootclip {
	const uint8_t *blob;
	size_t blob_len;
	uint32_t bg_rgb;	/* what the LVGL screen is filled with first */
	const char *name;	/* for the boot log, so a mis-provisioned unit
				 * says so over the cable instead of only on
				 * the panel */
};

const struct bootclip *bootclip_active(void);

#endif /* BOOTCLIP_H */
