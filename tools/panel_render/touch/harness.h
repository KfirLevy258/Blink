#ifndef TOUCH_HARNESS_H
#define TOUCH_HARNESS_H
#include <stdint.h>

/* One wire line to the bench, on stdout. */
void harness_emit(const char *json);
/* theme_now, as proto.c sends it. */
void harness_theme_now(const char *why);
/* proto.c's handling of ota_avail, ota_none and ota_error (stubs.c). */
void harness_ota_avail(const char *version, uint32_t size, const char *app);
void harness_ota_none(void);
void harness_ota_error(const char *why);
/* An RGB888 image as an uncompressed PNG. 0 on success. */
int png_write(const char *path, const uint8_t *rgb, int w, int h);

#endif
