/* Host stand-in for Zephyr's <zephyr/random/random.h>: the one call
 * wl_crypto_mbedtls.c makes. host_test.c defines it, so the test decides
 * whether the CSPRNG works. */
#ifndef STUB_ZEPHYR_RANDOM_H
#define STUB_ZEPHYR_RANDOM_H
#include <stddef.h>
int sys_csrand_get(void *dst, size_t len);
#endif
