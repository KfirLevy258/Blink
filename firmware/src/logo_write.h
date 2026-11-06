#ifndef LOGO_WRITE_H
#define LOGO_WRITE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * The company logo partition written from a stream (spec S15.2): logo_begin,
 * then the bytes in order (logo_chunk lines over USB, G frames over WiFi),
 * then logo_end, which runs the full logo_parse over what landed. The
 * OTA writer's shape (ota_net.c), on its own partition: no flash_img, that is
 * slot 1's.
 *
 * MAIN THREAD ONLY, like the OTA consumer (D34). No locking. Never logs a
 * byte, an offset or a length (CLAUDE.md).
 *
 * "Present" stays trustworthy, which is what logo_parse.h promises: every way
 * a stream ends without a logo that parses -- out of order, past the bound,
 * a flash error, an abort, a failed end -- leaves the partition erased.
 */

/* The partition's size (esp32_devkitc_esp32_procpu.overlay, `logo`), and so
 * the largest image; pc/logo_encode.LOGO_MAX mirrors it. */
#define LOGO_IMAGE_MAX 0x80000u

/* -EINVAL for 0 or over LOGO_IMAGE_MAX (nothing erased); -EIO if the erase
 * fails; else 0, the partition erased and the stream open. `crc` is zlib's
 * CRC-32 of the whole image, checked at end. Any stream open is dropped. */
int logo_write_begin(uint32_t size, uint32_t crc);

/* The bytes accepted so far, or -1 on a gap, a rewind, past `size`, or a
 * flash error. Every -1 has already dropped the stream and erased. */
int logo_write_chunk(uint32_t off, const uint8_t *b, size_t n);

/* 0 when every byte arrived, the stream's CRC matches and logo_parse accepts
 * the partition; anything else erases and returns non-zero. Closes the
 * stream either way. */
int logo_write_end(void);

/* Drops an open stream and erases what it wrote. Nothing open: no-op. */
void logo_write_abort(void);
bool logo_write_active(void);

/* Erases the partition: the unit is an individual one again. */
int logo_write_remove(void);

/* What the partition holds now, for logo_state: true and its image size and
 * frame count when a logo is present. Parsed once, then kept up to date by
 * the calls above. */
bool logo_write_state(uint32_t *bytes, uint16_t *frames);

/*
 * The port: the four things logo_write.c needs of the flash. The board's are
 * at the bottom of logo_write.c (flash_area and spi_flash_mmap); the host
 * tests' are tests/support/flash_area_fake.c, a RAM partition.
 */
int logo_part_erase(void);
int logo_part_write(uint32_t off, const uint8_t *b, size_t n);
const uint8_t *logo_part_map(void);	/* LOGO_IMAGE_MAX bytes, or NULL */
void logo_part_unmap(void);

#endif /* LOGO_WRITE_H */
