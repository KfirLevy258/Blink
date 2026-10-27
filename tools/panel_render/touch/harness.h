#ifndef TOUCH_HARNESS_H
#define TOUCH_HARNESS_H
#include <stdint.h>

/* One wire line to the bench, on stdout. */
void harness_emit(const char *json);
/* theme_now, as proto.c sends it. */
void harness_theme_now(const char *why);
/* An RGB888 image as an uncompressed PNG. 0 on success. */
int png_write(const char *path, const uint8_t *rgb, int w, int h);

#endif
