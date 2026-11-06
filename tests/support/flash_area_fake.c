/* logo_write.c's flash port on a laptop: the 512 KB logo partition in RAM,
 * and a record of what was done to it. NOT Zephyr's code, only the shape of
 * the four calls logo_write.c makes (logo_write.h, "the port"). Never link it
 * into firmware. */
#include <string.h>

#include "logo_write.h"

uint8_t fake_logo_part[LOGO_IMAGE_MAX];
int fake_logo_erases;		/* whole-partition erases */
int fake_logo_fail_write;	/* next write returns -5 and clears */
int fake_logo_maps;		/* map calls not yet unmapped */

void fake_logo_reset(void)
{
	memset(fake_logo_part, 0xff, sizeof(fake_logo_part));
	fake_logo_erases = 0;
	fake_logo_fail_write = 0;
	fake_logo_maps = 0;
}

int logo_part_erase(void)
{
	memset(fake_logo_part, 0xff, sizeof(fake_logo_part));
	fake_logo_erases++;
	return 0;
}

int logo_part_write(uint32_t off, const uint8_t *b, size_t n)
{
	if (fake_logo_fail_write) {
		fake_logo_fail_write = 0;
		return -5;
	}
	if (off > LOGO_IMAGE_MAX || n > LOGO_IMAGE_MAX - off) {
		return -22;	/* the real flash_area_write's bound */
	}
	memcpy(fake_logo_part + off, b, n);
	return 0;
}

const uint8_t *logo_part_map(void)
{
	fake_logo_maps++;
	return fake_logo_part;
}

void logo_part_unmap(void)
{
	fake_logo_maps--;
}
