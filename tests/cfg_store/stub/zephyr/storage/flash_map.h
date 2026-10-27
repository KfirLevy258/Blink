/* Host stub: the storage partition is a RAM array the test owns, so it can
 * plant a record written by older firmware and read back what this one
 * wrote. Erased bytes read 0xFF, as on an unencrypted chip. */
#ifndef CFG_STUB_FLASH_MAP_H
#define CFG_STUB_FLASH_MAP_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define FAKE_FLASH_SIZE 8192
extern uint8_t fake_flash[FAKE_FLASH_SIZE];
/* Bytes a write may still put down before the power goes: -1 is no cut.
 * And a count of writes, to prove one was skipped. */
extern long fake_write_budget;
extern int fake_writes;

struct flash_area { int unused; };
#define FIXED_PARTITION_ID(x) 0

static inline int flash_area_open(int id, const struct flash_area **fa)
{
	static const struct flash_area one;

	(void)id;
	*fa = &one;
	return 0;
}
static inline int flash_area_read(const struct flash_area *fa, long off,
				  void *dst, size_t len)
{
	(void)fa;
	if (off < 0 || off + (long)len > FAKE_FLASH_SIZE) {
		return -1;
	}
	memcpy(dst, fake_flash + off, len);
	return 0;
}
static inline int flash_area_write(const struct flash_area *fa, long off,
				   const void *src, size_t len)
{
	(void)fa;
	if (off < 0 || off + (long)len > FAKE_FLASH_SIZE) {
		return -1;
	}
	fake_writes++;
	if (fake_write_budget >= 0 && (long)len > fake_write_budget) {
		len = (size_t)fake_write_budget;	/* the power went here */
	}
	memcpy(fake_flash + off, src, len);
	if (fake_write_budget >= 0) {
		fake_write_budget -= (long)len;
	}
	return 0;
}
static inline int flash_area_erase(const struct flash_area *fa, long off,
				   size_t len)
{
	(void)fa;
	if (off < 0 || off + (long)len > FAKE_FLASH_SIZE) {
		return -1;
	}
	memset(fake_flash + off, 0xFF, len);
	return 0;
}
#endif
